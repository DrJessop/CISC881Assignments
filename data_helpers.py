import torch
import torch.nn as nn
from torch.utils.data import Dataset
import os
import SimpleITK as sitk
import numpy as np
import adabound
import matplotlib.pyplot as plt
from sklearn.metrics import f1_score, auc, roc_curve, roc_auc_score, confusion_matrix
import random
from image_augmentation import rotation3d
import shutil
import pandas as pd
import copy
from models import CNN2


def resample_image(itk_image, out_spacing, is_label=False):
    """
    Retrieved this function from:
    https://www.programcreek.com/python/example/96383/SimpleITK.sitkNearestNeighbor
    :param itk_image: The image that we would like to resample
    :param out_spacing: The new spacing of the voxels we would like
    :param is_label: If True, use kNearestNeighbour interpolation, else use BSpline
    :return: The re-sampled image
    """

    original_spacing = itk_image.GetSpacing()
    original_size = itk_image.GetSize()

    out_size = [int(np.round(original_size[0]*(original_spacing[0]/out_spacing[0]))),
                int(np.round(original_size[1]*(original_spacing[1]/out_spacing[1]))),
                int(np.round(original_size[2]*(original_spacing[2]/out_spacing[2])))]

    resample = sitk.ResampleImageFilter()
    resample.SetOutputSpacing(out_spacing)
    resample.SetSize(out_size)
    resample.SetOutputDirection(itk_image.GetDirection())
    resample.SetOutputOrigin(itk_image.GetOrigin())
    resample.SetTransform(sitk.Transform())
    resample.SetDefaultPixelValue(itk_image.GetPixelIDValue())

    if is_label:
        resample.SetInterpolator(sitk.sitkNearestNeighbor)
    else:
        resample.SetInterpolator(sitk.sitkCosineWindowedSinc)

    return resample.Execute(itk_image)


def resample_all_images(modality, out_spacing, some_missing=False):
    """
    This function returns a list of re-sampled images for a given modality and desired spacing
    :param modality: ex. t2, adc, bval, etc.
    :param out_spacing: The desired spacing of the images
    :param some_missing: If an image may be missing, this may be set to True to handle the case
    of a missing image
    :return: Re-sampled images
    """

    if some_missing:
        return [resample_image(mod_image, out_spacing) if mod_image != "" else ""
                for mod_image in modality]
    return [resample_image(mod_image, out_spacing)
            if mod_image != "" else "" for mod_image in modality]


def crop_from_center(images, ijk_coordinates, width, height, depth, i_offset=0, j_offset=0):
    """
    Helper function for image cropper and rotated crop that produces a crop of dimension width x height x depth,
    where the lesion is offset by i_offset (x dimension) and j_offset (y dimension)
    :param images: A list of size 3 tuples, where the elements in the tuple are t2, adc, and bval SITK images
                   respectively
    :param ijk_coordinates: The coordinates of the lesion
    :param width: Desired width of the crop
    :param height: Desired height of the crop
    :param depth: Desired depth of the crop
    :param i_offset: Desired offset in pixels away from the lesion in the x direction
    :param j_offset: Desired offset in pixels away from the lesion in the y direction
    :return: The newly created crop
    """
    crop = [image[(ijk_coordinates[idx][0] - i_offset) - width // 2: (ijk_coordinates[idx][0] - i_offset)
                  + int(np.ceil(width / 2)),
                  (ijk_coordinates[idx][1] - j_offset) - height // 2: (ijk_coordinates[idx][1] - j_offset)
                  + int(np.ceil(height / 2)),
                  ijk_coordinates[idx][2] - depth // 2: ijk_coordinates[idx][2]
                  + int(np.ceil(depth / 2))]
            for idx, image in enumerate(images)]
    return crop


def rotated_crop(patient_images, crop_width, crop_height, crop_depth, degrees, lps, ijk_values, show_result=False):
    """
    This is a helper function for image_cropper. It rotates and translates the given images, and then crops them
    from the center.
    :param patient_image: The sitk image that is to be cropped
    :param crop_width: The desired width of the crop
    :param crop_height: The desired height of the crop
    :param crop_depth: The desired depth of the crop
    :param degrees: A list of all allowable degrees of rotation (gets converted to radians in the rotation3d function
                    which is called below)
    :param lps: The region of interest which will be the center of rotation
    :param ijk_values: A list of lists, where each list is the ijk values for each image's biopsy position
    :param show_result: Whether or not the user wants to see the first slice of the new results
    :return: The crop of the rotated image
    """

    degree = np.random.choice(degrees)
    rotated_patient_images = list(map(lambda patient: rotation3d(patient, degree, lps), patient_images))

    i_offset = np.random.randint(-7, 7)
    j_offset = np.random.randint(-7, 7)

    crop = crop_from_center(rotated_patient_images, ijk_values, crop_width, crop_height, crop_depth, i_offset=i_offset,
                            j_offset=j_offset)

    if show_result:
        for i in range(3):
            plt.imshow(sitk.GetArrayFromImage(rotated_patient_images[0])[0], cmap="gray")
            plt.imshow(sitk.GetArrayFromImage(crop[i])[0], cmap="gray")
            plt.show()
        input()
    return crop


def write_cropped_images_train_and_folds(cropped_images, num_crops, num_folds=5, fold_fraction=0.2):
    """
    This function writes all cropped images to a training directory (for each modality) and creates a list of hashmaps
    for folds. These maps ensure that there is a balanced distribution of cancer and non-cancer in each validation set
    as well as the training set used for prediction.
    :param cropped_images: A dictionary where the keys are the patient IDs, and the values are lists where each element
    is a list of length three (first element in that list is t2 image, and then adc and bval).
    :param num_crops: The number of crops for a given patient's image
    :param num_folds: The number of sets to be created
    :param fold_fraction: The amount of cancer patients to be within a fold's validation set
    :return: fold key and train key mappings (lists of hash functions which map to the correct patient data)
    """

    destination = r"/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/"

    directory_contents = os.listdir(destination)
    for sub_directory in directory_contents:
        sub_directory_path = destination + sub_directory
        shutil.rmtree(sub_directory_path)
        os.mkdir(sub_directory_path)

    destination = destination + r"{}/{}_{}.nrrd"

    patient_images = [(key, patient_image) for key in cropped_images.keys()
                      for patient_image in cropped_images[key]]

    for p_id in range(len(patient_images)):
        _, (patient_image, cancer_marker) = patient_images[p_id]
        sitk.WriteImage(patient_image[0], destination.format("t2", p_id, cancer_marker))
        sitk.WriteImage(patient_image[1], destination.format("adc", p_id, cancer_marker))
        sitk.WriteImage(patient_image[2], destination.format("bval", p_id, cancer_marker))

    patient_indices = set(range(len(patient_images) // num_crops))
    non_cancer_patients = {idx for idx in patient_indices if patient_images[idx * num_crops][0][-1] == '0'}
    cancer_patients = {idx for idx in patient_indices if patient_images[idx * num_crops][0][-1] == '1'}

    num_each_class_fold = int(fold_fraction * len(cancer_patients))

    fold_key_mappings = []
    train_key_mappings = []
    for k in range(num_folds):

        non_cancer_in_fold = random.sample(non_cancer_patients, num_each_class_fold)
        cancer_in_fold = random.sample(cancer_patients, num_each_class_fold)

        fold_set = set()
        fold_set.update(non_cancer_in_fold)
        fold_set.update(cancer_in_fold)

        out_of_fold = patient_indices.difference(fold_set)

        # Uses up all the cancer patients
        cancer_out_of_fold = {idx for idx in out_of_fold if patient_images[idx * num_crops][0][-1] == '1'}
        non_cancer_out_of_fold = random.sample(out_of_fold.difference(cancer_out_of_fold), len(cancer_out_of_fold))

        out_of_fold_set = set()
        out_of_fold_set.update(cancer_out_of_fold)
        out_of_fold_set.update(non_cancer_out_of_fold)

        # Prepare fold indices
        fold_image_indices = set()
        for key in fold_set:
            image_index = key * num_crops
            for pos in range(num_crops):
                fold_image_indices.add(image_index + pos)

        # Prepare train key indices
        out_of_fold_image_indices = set()
        for key in out_of_fold_set:
            image_index = key * num_crops
            for pos in range(num_crops):
                out_of_fold_image_indices.add(image_index + pos)

        fold_key_mapping = {}
        key = 0
        for fold_image_index in fold_image_indices:
            fold_key_mapping[key] = fold_image_index
            key += 1

        train_key_mapping = {}
        key = 0
        for train_image_index in out_of_fold_image_indices:
            train_key_mapping[key] = train_image_index
            key += 1

        fold_key_mappings.append(fold_key_mapping)
        train_key_mappings.append(train_key_mapping)

    return fold_key_mappings, train_key_mappings


def image_cropper(findings_dataframe, resampled_images, padding,
                  crop_width, crop_height, crop_depth, num_crops_per_image=1, train=True):
    """
    Given a dataframe with the findings of cancer, a list of images, and a desired width, height,
    and depth, this function returns a set of cropped versions of the original images of dimension
    crop_width x crop_height x crop_depth
    :param findings_dataframe: A pandas dataframe containing the LPS coordinates of the cancer
    :param resampled_images: A list of images that have been resampled to all have the same
                             spacing
    :param padding: 0-Padding in the i,j,k directions
    :param crop_width: The desired width of a patch
    :param crop_height: The desired height of a patch
    :param crop_depth: The desired depth of a patch
    :param num_crops_per_image: The number of crops desired for a given image
    :param train: Boolean, represents whether these are crops of the training or the test set
    :return: A list of cropped versions of the original re-sampled images
    """

    t2_resampled, adc_resampled, bval_resampled = resampled_images

    if num_crops_per_image < 1:
        print("Cannot have less than 1 crop for an image")
        exit()
    degrees = [5, 10, 15, 20, 25]  # One of these is randomly chosen for every rotated crop
    degrees = [-degree for degree in degrees] + degrees
    crops = {}
    invalid_keys = set()
    for _, patient in findings_dataframe.iterrows():
        patient_id = patient["patient_id"]
        patient_images = [t2_resampled[int(patient_id[-4:])], adc_resampled[int(patient_id[-4:])],
                          bval_resampled[int(patient_id[-4:])]]
        if train:
            cancer_marker = int(patient["ClinSig"])  # 1 if cancer, else 0
        if '' in patient_images:  # One of the images is blank
            continue
        else:
            # Adds padding to each of the images
            patient_images = [padding.Execute(p_image) for p_image in patient_images]
            lps = [float(loc) for loc in patient["pos"].split(' ') if loc != '']

            # Convert lps to ijk for each of the images
            ijk_vals = [patient_images[idx].TransformPhysicalPointToIndex(lps) for idx in range(3)]

            # Below code makes a crop of dimensions crop_width x crop_height x crop_depth
            for crop_num in range(num_crops_per_image):
                if crop_num == 0:  # The first crop we want to guarantee has the biopsy position exactly in the center
                    crop = crop_from_center(patient_images, ijk_vals, crop_width, crop_height, crop_depth)
                else:
                    # Rotate the image, and then translate and crop
                    crop = rotated_crop(patient_images, crop_width, crop_height, crop_depth, degrees, lps, ijk_vals)
                invalid_sizes = [im.GetSize() for im in crop if im.GetSize() != (crop_width, crop_height, crop_depth)]
                if train:
                    if invalid_sizes:  # If not all of the image sizes are correct
                        print("Invalid image for patient {}".format(patient_id))
                        invalid_keys.add("{}_{}".format(patient_id, cancer_marker))
                        continue
                    # If any of the crops are bad, they're all bad
                    elif np.sum(sitk.GetArrayFromImage(crop[0]).flatten()) == 0:
                        invalid_keys.add("{}_{}".format(patient_id, cancer_marker))
                else:
                    print(np.sum(sitk.GetArrayFromImage(crop[2]).flatten()))
                    if np.sum(sitk.GetArrayFromImage(crop[0]).flatten()) == 0:
                        crop = [sitk.GetImageFromArray(np.random.rand(crop_depth, crop_height, crop_width))
                                for _ in range(3)]
                        print(patient_id)
                if train:
                    key = "{}_{}".format(patient_id, cancer_marker)
                else:
                    key = patient_id
                if key in crops.keys():
                    if train:
                        crops[key].append((crop, cancer_marker))
                    else:
                        crops[key].append(crop)
                else:
                    if train:
                        crops[key] = [(crop, cancer_marker)]
                    else:
                        crops[key] = [crop]

    for key in invalid_keys:
        crops.pop(key)
    return crops


class ProstateImages(Dataset):
    """
    This class's sole purpose is to provide the framework for fetching training/test data for the data loader which
    uses this class as a parameter
    """

    def __init__(self, modality, train, device, normalize_strategy=1, mapping=None):
        assert modality in ["t2", "bval", "adc"]
        assert normalize_strategy in [1, 2]
        self.modality = modality
        self.train = train
        self.device = device
        self.normalize_strategy = normalize_strategy
        if self.normalize_strategy == 1:
            self.normalize = sitk.NormalizeImageFilter()
        else:
            if self.modality == "adc":
                mean_path = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/adc_mean_tensor.npy"
                std_path = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/adc_std_tensor.npy"
            elif self.modality == "bval":
                mean_path = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/bval_mean_tensor.npy"
                std_path = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/bval_std_tensor.npy"
            else:
                mean_path = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/t2_mean_tensor.npy"
                std_path = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/t2_std_tensor.npy"
            self.mean_tensor = np.load(mean_path)
            self.std_tensor = np.load(std_path)

        if self.train:
            self.mapping = mapping
            self.map_num = 0

            # The 0th index may vary depending on the first key of the hash function
            self.first_index = sorted(self.mapping[self.map_num])[0]
            self.length = len(self.mapping[self.map_num])
        else:
            path = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/test/{}/".format(self.modality)
            sorted_path = sorted(os.listdir(path))
            self.length = len(sorted_path)
            self.first_index = int(sorted_path[0].split('.')[0])

    def __len__(self):
        return self.length

    def change_map_num(self, new_map_num):
        self.map_num = new_map_num
        self.first_index = sorted(self.mapping[self.map_num])[0]
        self.length = len(self.mapping[self.map_num])

    def __getitem__(self, index):
        if self.train:
            index = self.mapping[self.map_num][index + self.first_index]
            path = "{}/{}".format(
                "/home/andrewg/PycharmProjects/assignments/resampled_cropped/train",
                "{}/{}_{}.nrrd".format(self.modality, index, "{}")
            )
            try:
                final_path = path.format(0)
                image = sitk.ReadImage(final_path)
            except:
                final_path = path.format(1)
                image = sitk.ReadImage(final_path)

            # The last digit in the file name specifies cancer/non-cancer
            cancer_label = int(final_path.split('.')[0][-1])
            output = {"image": image, "cancer": cancer_label}

        else:
            index = self.first_index + index
            path = "{}/{}".format(
                "/home/andrewg/PycharmProjects/assignments/resampled_cropped/test",
                "{}/{}.nrrd".format(self.modality, index)
            )
            image = sitk.ReadImage(path)
            output = {"image": image}

        if self.normalize_strategy == 1:
            output["image"] = self.normalize.Execute(output["image"])
        output["image"] = sitk.GetArrayFromImage(output["image"])

        if self.normalize_strategy == 2:
            output["image"] = (output["image"] - self.mean_tensor) / self.std_tensor

        # Completely black or white images have no standard deviation, results in nan
        if np.isnan(output["image"][0, 0, 0]):
            output["image"] = np.random.rand(3, 32, 32)
        output["image"] = torch.from_numpy(output["image"]).float().to(self.device)
        output["index"] = index
        return output


def he_initialize(model):
    """
    He weight initialization, as described in Delving Deep into Rectifiers:Surpassing Human-Level Performance on
    ImageNet Classification (https://arxiv.org/pdf/1502.01852.pdf)
    :param model: The network being initialized
    :return: None
    """
    if isinstance(model, nn.Conv2d):
        torch.nn.init.kaiming_normal_(model.weight)
        if model.bias:
            torch.nn.init.kaiming_normal_(model.bias)
    if isinstance(model, nn.Linear):
        torch.nn.init.kaiming_normal_(model.weight)
        if model.bias:
            torch.nn.init.kaiming_normal_(model.bias)


def flatten_batch(image_shape, images, class_vector, cuda_destination):
    """
    For example, if you have shape [batch_size, num_images_per_patient, width, height, length], then
    this makes duplicates such that if a patient has cancer, instead of having 1 cancer label, they
    have num_images_per_patient cancer labels
    ex. if num_images_per_patient = 3, and class_vector is [1,1,0], this then becomes
    [1,1,1, 1,1,1, 0,0,0]
    :param image_shape: The total shape of the batch
    :param images: Batches of images with one extra dimension
    :param class_vector: Cancer label vector
    :param cuda_destination: The gpu that the model is using
    :return: The flattened images and the lengthened class vector
    """

    class_vector = torch.tensor([[class_vector[idx]] * image_shape[1] for idx in
                                 range(image_shape[0])]).float().cuda(cuda_destination).unsqueeze(1)
    class_vector = class_vector.view(-1).unsqueeze(1)
    images = images.view(images.shape[0] * images.shape[1], *images.shape[2:])
    return images, class_vector


def train_model(train_data, val_data, model, epochs, optimizer, loss_function, softmax=False, show=False):
    """
    This function trains a model with batches of a given size, and if show=True, plots the loss, f1, and auc scores for
    the training and validation sets
    :param train_data: A dataloader containing batches of the training set
    :param val_data: A dataloader containing batches of the validation set
    :param model: The network being trained
    :param epochs: How many times the user wants the model trained on all of the training set data
    :param batch_size: How many data points are in a batch
    :param optimizer: Method used to update the network's weights
    :param loss_function: How the model will be evaluated
    :param num_folds: How many folds were chosen to be
    :param show: Whether or not the user wants to see plots of loss, f1, and auc scores for the training and validation
                 sets
    :return: AUC and F1 train, AUC and F1 validation
    """

    if softmax:
        initialize_CNN2(model, "bval")
    errors = []
    eval_errors = []
    f1_train = []
    auc_train = []
    f1_eval = []
    auc_eval = []
    num_training_batches = len(train_data)
    best_auc = 0
    for epoch in range(epochs):
        model.train()  # Training mode
        train_iter = iter(train_data)
        model.zero_grad()
        train_loss = 0
        all_preds = []
        all_actual = []
        for batch_num in range(num_training_batches):
            batch = next(train_iter)
            images, class_vector = batch["image"], batch["cancer"].float().cuda(model.cuda_destination).unsqueeze(1)
            image_shape = images.shape
            if len(image_shape) != 4:
                images, class_vector = flatten_batch(image_shape, images, class_vector, model.cuda_destination)
            optimizer.zero_grad()
            preds = model(images)

            class_vector = class_vector.squeeze(1)

            if softmax:
                class_vector = class_vector.long()

            loss = loss_function(preds, class_vector)

            if softmax:
                hard_preds = torch.tensor([torch.argmax(tup) for tup in preds])
            else:
                hard_preds = torch.round(preds)

            all_preds.extend(hard_preds.squeeze(-1).tolist())
            try:
                all_actual.extend(class_vector.squeeze(-1).tolist())
            except:
                all_actual.append(class_vector.squeeze(-1).tolist())
            train_loss += loss.item()
            loss.backward()
            optimizer.step()
        train_loss_avg = train_loss / num_training_batches
        f1_train.append(f1_score(all_actual, all_preds))
        fpr, tpr, _ = roc_curve(all_actual, all_preds, pos_label=1)
        auc_train.append(auc(fpr, tpr))
        errors.append(train_loss_avg)
        model.eval()  # Evaluation mode
        num_val_batches = len(val_data)
        val_iter = iter(val_data)
        eval_loss = 0
        all_preds = []
        all_actual = []
        with torch.no_grad():
            for batch_num in range(num_val_batches):
                batch = next(val_iter)
                images, class_vector = batch["image"], batch["cancer"].float().cuda(model.cuda_destination).unsqueeze(1)
                image_shape = images.shape
                if len(image_shape) != 4:
                    images, class_vector = flatten_batch(image_shape, images, class_vector, model.cuda_destination)
                preds = model(images)
                class_vector = class_vector.squeeze(1)
                if softmax:
                    class_vector = class_vector.long()
                loss = loss_function(preds, class_vector)
                eval_loss += loss.item()
                if softmax:
                    hard_preds = torch.tensor([torch.argmax(tup) for tup in preds])
                else:
                    hard_preds = torch.round(preds)
                all_preds.extend(hard_preds.squeeze(-1).tolist())
                try:
                    all_actual.extend(class_vector.squeeze(-1).tolist())
                except:
                    all_actual.append(class_vector.squeeze(-1).tolist())

        eval_loss_avg = eval_loss / num_val_batches
        print("Loss Epoch {}, Training: {}, Validation: {}".format(epoch + 1, train_loss_avg, eval_loss_avg))
        f1_eval.append(f1_score(all_actual, all_preds))
        fpr, tpr, _ = roc_curve(all_actual, all_preds, pos_label=1)
        auc_eval.append(auc(fpr, tpr))

        if auc_eval[-1] > best_auc:
            auc_train_of_best_model = auc_train[-1]
            f1_train_of_best_model = f1_train[-1]

            conf_matrix = confusion_matrix(all_actual, all_preds)
            best_auc = auc_eval[-1]
            best_f1 = f1_eval[-1]
            best_model = copy.deepcopy(model)
        eval_errors.append(eval_loss_avg)

    if show:
        plt.plot(errors)
        plt.plot(eval_errors)
        plt.title("Training (blue) vs Cross-Validation (orange) Error (BCELoss)")
        plt.legend(["training loss", "validation loss"])
        plt.show()
        plt.plot(f1_train)
        plt.plot(f1_eval)
        plt.legend(["training f1", "validation f1"])
        plt.title("F1 Training vs F1 Cross-Validation")
        plt.show()
        plt.plot(auc_train)
        plt.plot(auc_eval)
        plt.legend(["training auc", "validation auc"])
        plt.title("AUC Training vs AUC Cross-Validation")
        plt.show()

    print("The best AUC on the validation set during training was {}".format(best_auc))
    return best_model, conf_matrix, auc_train_of_best_model, f1_train_of_best_model, best_auc, best_f1


def k_fold_cross_validation(network, k_low, k_high, train_data, val_data, epochs, loss_function, device, lr=0.005,
                            final_lr=0.05, momentum=0.9, weight_decay=0.04, softmax=False,
                            show=True, cuda_destination=1):
    """
    Given training and validation data, performs K-fold cross-validation.
    :param network: Instance of the class you will use as the network
    :param K: Number of folds
    :param train_data: A tuple containing a ProstateImages object where train=True and a dataloader in which
                       the ProstateImages object is supplied as a parameter
    :param val_data: A tuple containing a ProstateImages object where train=True and a dataloader in which
                     the ProstateImages object is supplied as a parameter
    :param epochs: The number of epochs each model is to be trained for
    :param loss_function: The desired loss function which is to be used by every model being trained
    :param lr: The learning rate, default is 0.005
    :param momentum: The momentum for stochastic gradient descent, default is 0.9
    :param weight_decay: L2 regularization alpha parameter, default is 0.06
    :param show: Whether or not to show the train/val loss, f1, and auc curves after each fold, default is True
    :param cuda_destination: The GPU that is used by the model
    :return: A list (size 4) of lists, where the first list contains the auc scores for the training sets, the second
             list contains the f1 scores for the training sets, the third list contains the auc scores for the
             validation sets, and the fourth and final list contains the f1 scores for the validation sets
    """
    train_data, train_dataloader = train_data
    val_data, val_dataloader = val_data
    auc_train_avg, f1_train_avg, auc_eval_avg, f1_eval_avg = [], [], [], []
    models = []
    model = network(cuda_destination)
    model.cuda(model.cuda_destination)
    model.load_state_dict(torch.load(
        "/home/andrewg/PycharmProjects/assignments/predictions/models/{}/{}/{}.pt".format("bval", "CNN",
                                                                                          1),
        map_location=device))
    for k in range(k_low, k_high):
        print("Fold {}".format(k + 1))
        # model = network(cuda_destination)
        # model.cuda(model.cuda_destination)
        # optimizer = optim.SGD(model.parameters(), lr=lr, momentum=momentum, weight_decay=weight_decay)
        # optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
        optimizer = adabound.AdaBound(model.parameters(), lr=lr, final_lr=final_lr, weight_decay=weight_decay)
        train_data.change_map_num(k)
        val_data.change_map_num(k)
        model, _, auc_train, f1_train, auc_eval, f1_eval = train_model(train_dataloader, val_dataloader, model, epochs,
                                                                       optimizer, loss_function, softmax=softmax,
                                                                       show=True)
        auc_train_avg.append(auc_train)
        f1_train_avg.append(f1_train)
        auc_eval_avg.append(auc_eval)
        f1_eval_avg.append(f1_eval)
        models.append(model)

    scores = [auc_train_avg, f1_train_avg, auc_eval_avg, f1_eval_avg]
    if show:
        print(scores)

    with open("/home/andrewg/PycharmProjects/assignments/predictions/k_fold_statistics/stats.txt", "a") as f:
        f.write("AUC train average\n")
        for avg in auc_train_avg:
            f.write(str(avg) + '\n')
        f.write("F1 train average\n")
        for avg in f1_train_avg:
            f.write(str(avg) + '\n')
        f.write("AUC eval average\n")
        for avg in auc_eval_avg:
            f.write(str(avg) + '\n')
        f.write("F1 eval average\n")
        for avg in f1_eval_avg:
            f.write(str(avg) + '\n')
    return list(zip(models, scores))


def generate_random_resampled_number():
    """
    This function generates a random file number (in string form) from the resampled directory
    :return: A number in the form ABCD
    """
    file_number = str(random.randint(0, 345))
    return "".join(["0" for _ in range(4 - len(file_number))]) + file_number


def create_kgh_patient_crops(num_crops, crop_dim):
    """
    This function produces a dictionary of cropped images from the KGH data
    :param num_crops: The desired number of crops for a given image
    :param crop_dim: The desired dimensions of a crop given as a tuple (width x height x depth)
    :return: A dictionary where the keys are the patient numbers and the values are the cropped images
    """
    kgh_data_dir = "/home/andrewg/PycharmProjects/assignments/data/KGHData"
    directories = os.listdir(kgh_data_dir)
    bval = dict()
    adc = dict()
    t2 = dict()
    degrees = [i for i in range(26)]
    matching_filter = sitk.HistogramMatchingImageFilter()
    for directory in directories:
        sub_directory = "{}/{}".format(kgh_data_dir, directory)
        if not(os.path.isdir(sub_directory)):
            continue
        sub_directory_contents = os.listdir(sub_directory)
        bval_nrrd_file, adc_nrrd_file, t2_nrrd_file = None, None, None
        for file in sub_directory_contents:
            if "nrrd" in file and "bval" in file:
                bval_nrrd_file = file  # file becomes the nrrd file name at this point
            if "nrrd" in file and "adc" in file:
                adc_nrrd_file = file
            if "nrrd" in file and "t2" in file:
                t2_nrrd_file = file

        if not bval_nrrd_file or not adc_nrrd_file or not t2_nrrd_file:
            continue
        bval_nrrd_file = "{}/{}".format(sub_directory, bval_nrrd_file)
        adc_nrrd_file = "{}/{}".format(sub_directory, adc_nrrd_file)
        t2_nrrd_file = "{}/{}".format(sub_directory, t2_nrrd_file)
        try:
            with open("{}/fiducials/{}".format(kgh_data_dir, directory)) as fid_file:
                bval[directory] = [resample_image(sitk.ReadImage(bval_nrrd_file), out_spacing=(2, 2, 3))]
                adc[directory] = [resample_image(sitk.ReadImage(adc_nrrd_file), out_spacing=(2, 2, 3))]
                t2[directory] = [resample_image(sitk.ReadImage(t2_nrrd_file), out_spacing=(2, 2, 3))]

                # random_number = generate_random_resampled_number()

                # random_bval = "/home/andrewg/PycharmProjects/assignments/resampled/bval/ProstateX-{}.nrrd".format(
                #                 random_number)
                # random_adc = "/home/andrewg/PycharmProjects/assignments/resampled/adc/ProstateX-{}.nrrd".format(
                #                 random_number)
                # random_t2 = "/home/andrewg/PycharmProjects/assignments/resampled/t2/ProstateX-{}.nrrd".format(
                #                 random_number)

                # bval[directory][0] = matching_filter.Execute(bval[directory][0], sitk.ReadImage(random_bval))
                # adc[directory][0] = matching_filter.Execute(adc[directory][0], sitk.ReadImage(random_adc))
                # t2[directory][0] = matching_filter.Execute(t2[directory][0], sitk.ReadImage(random_t2))

                for idx, line in enumerate(fid_file):
                    if idx == 3:
                        fiducial = list(map(float, line.split(',')[1:4]))
                        fiducial[0], fiducial[1] = -fiducial[0], -fiducial[1]
                        ijk_bval = bval[directory][0].TransformPhysicalPointToIndex(fiducial)
                        center_crop_bval = crop_from_center([bval[directory][0]], [ijk_bval], *crop_dim, i_offset=0,
                                                            j_offset=0)[0]

                        ijk_adc = adc[directory][0].TransformPhysicalPointToIndex(fiducial)
                        center_crop_adc = crop_from_center([adc[directory][0]], [ijk_adc], *crop_dim, i_offset=0,
                                                           j_offset=0)[0]

                        ijk_t2 = t2[directory][0].TransformPhysicalPointToIndex(fiducial)
                        center_crop_t2 = crop_from_center([t2[directory][0]], [ijk_t2], *crop_dim, i_offset=0,
                                                          j_offset=0)[0]

                        bval[directory].append(center_crop_bval)
                        adc[directory].append(center_crop_adc)
                        t2[directory].append(center_crop_t2)

                        for crop_num in range(1, num_crops):
                            rotated_image_bval = rotated_crop([bval[directory][0]], *crop_dim, degrees, fiducial,
                                                         [ijk_bval])[0]
                            bval[directory].append(rotated_image_bval)

                            rotated_image_adc = rotated_crop([adc[directory][0]], *crop_dim, degrees, fiducial,
                                                         [ijk_adc])[0]
                            adc[directory].append(rotated_image_adc)

                            rotated_image_t2 = rotated_crop([t2[directory][0]], *crop_dim, degrees, fiducial,
                                                         [ijk_t2])[0]
                            t2[directory].append(rotated_image_t2)
        except:
            print(directory)
    return bval, adc, t2


class KGHProstateImages(Dataset):
    def __init__(self, device, modality):
        self.dir = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/kgh/{}".format(modality)
        self.folders = os.listdir(self.dir)
        self.length = len(self.folders)
        self.images = ["{}.nrrd".format(x) for x in range(len(os.listdir("{}/{}".format(self.dir, self.folders[0]))))]
        self.images.sort(key=lambda x: int(x.split('.')[0]))
        kgh_labels_file = "/home/andrewg/PycharmProjects/assignments/data/KGHData/kgh.csv"
        cancer_labels = pd.read_csv(kgh_labels_file)[["anonymized", "Total Gleason Xypeguide"]]
        cancer_labels = cancer_labels.drop([0, 7, 9, 14, 18, 22, 35, 71, 73, 81, 82, 83])
        for idx, val in cancer_labels["Total Gleason Xypeguide"].iteritems():
            if val == '0':
                cancer_labels["Total Gleason Xypeguide"][idx] = 0
            elif str(val) in '123456789':
                cancer_labels["Total Gleason Xypeguide"][idx] = 1
            else:
                cancer_labels["Total Gleason Xypeguide"][idx] = 0
        valid = set(cancer_labels[cancer_labels["Total Gleason Xypeguide"] == 0].index)
        valid.update(cancer_labels[cancer_labels["Total Gleason Xypeguide"] == 1].index)
        self.csv = cancer_labels.loc[valid]
        self.normalize = sitk.NormalizeImageFilter()
        self.device = device

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        patient_id = self.folders[idx]
        image_dir = "{}/{}".format(self.dir, patient_id)

        image_tensor = torch.from_numpy(np.asarray([sitk.GetArrayFromImage(
                       self.normalize.Execute(sitk.ReadImage("{}/{}".format(image_dir, image)))
                       ) for image in self.images])).float().to(self.device)
        cancer_label = self.csv.loc[self.csv.anonymized == '_'.join(patient_id.split('_')[:2])]
        cancer_label = int(cancer_label["Total Gleason Xypeguide"])
        return {"image": image_tensor, "cancer": cancer_label, "index": self.folders[idx]}


class KGHProstateImagesV2(Dataset):
    def __init__(self, device, modality, num_crops_per_image):
        self.num_crops = num_crops_per_image
        self.dir = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/kgh/{}".format(modality)
        self.folders = os.listdir(self.dir)
        self.length = len(self.folders) * num_crops_per_image
        self.images = ["{}.nrrd".format(x) for x in range(len(os.listdir("{}/{}".format(self.dir, self.folders[0]))))]
        self.images.sort(key=lambda x: int(x.split('.')[0]))
        kgh_labels_file = "/home/andrewg/PycharmProjects/assignments/data/KGHData/kgh.csv"
        cancer_labels = pd.read_csv(kgh_labels_file)[["anonymized", "Total Gleason Xypeguide"]]
        cancer_labels = cancer_labels.drop([0, 7, 9, 14, 18, 22, 35, 71, 73, 81, 82, 83])
        for idx, val in cancer_labels["Total Gleason Xypeguide"].iteritems():
            if val == '0':
                cancer_labels["Total Gleason Xypeguide"][idx] = 0
            elif str(val) in '123456789':
                cancer_labels["Total Gleason Xypeguide"][idx] = 1
            else:
                cancer_labels["Total Gleason Xypeguide"][idx] = 0
        valid = set(cancer_labels[cancer_labels["Total Gleason Xypeguide"] == 0].index)
        valid.update(cancer_labels[cancer_labels["Total Gleason Xypeguide"] == 1].index)
        self.csv = cancer_labels.loc[valid]
        self.normalize = sitk.NormalizeImageFilter()
        self.device = device

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        crop_id = idx % 20
        p_id = idx // 20
        patient_id = self.folders[p_id]
        image_dir = "{}/{}".format(self.dir, patient_id)

        image_tensor = torch.from_numpy(np.asarray([sitk.GetArrayFromImage(
                       self.normalize.Execute(sitk.ReadImage("{}/{}".format(image_dir, self.images[crop_id])))
                       )])).float().to(self.device)
        cancer_label = self.csv.loc[self.csv.anonymized == '_'.join(patient_id.split('_')[:2])]
        cancer_label = torch.tensor([int(cancer_label["Total Gleason Xypeguide"])])
        return {"image": image_tensor, "cancer": cancer_label, "index": patient_id}


def change_requires_grad(model, first_n_layers, new_grad):
    """
    This function either turns off or the turns on the gradient for 'first_n_layers' layers
    :param model: The model we would like to freeze/unfreeze some of the layers for
    :param first_n_layers: The number of layers we would like to freeze/unfreeze, starting from the first layer
    :param new_grad: If true, this unfreezes the first 'first_n_layers' layers, else freezes them
    :return: None
    """
    parameters = model.children()
    for idx in range(first_n_layers):
        child = next(parameters, None)
        if child:
            child.requires_grad = new_grad
            if "bias" in dir(child):
                child.bias.requires_grad = new_grad
        else:
            print("There were only {} layers".format(first_n_layers - 1))
            return


def bootstrap_auc(y_true, y_pred, ax, nsamples=1000):

    from scipy.interpolate import interp1d

    auc_values = []

    tpr_values = []

    for b in range(nsamples):

        idx = np.random.randint(y_true.shape[0], size=y_true.shape[0])
        y_true_bs = y_true[idx]
        y_pred_bs = y_pred[idx]
        fpr, tpr, _ = roc_curve(y_true_bs, y_pred_bs, drop_intermediate=True)

        if b == 0:
            fpr_interp = fpr

        f = interp1d(fpr, tpr)
        tpr_interp = f(fpr_interp)
        roc_auc = roc_auc_score(y_true_bs, y_pred_bs)
        auc_values.append(roc_auc)
        tpr_values.append(tpr_interp)

    auc_ci = np.percentile(auc_values, (2.5, 97.5))
    auc_mean = np.mean(auc_values)
    tprs_ci = np.percentile(tpr_values, (2.5, 97.5), axis=0)
    tprs_mean = np.mean(tpr_values, axis=0)
    ax.fill_between(fpr_interp, tprs_ci[0], tprs_ci[1], color='k', alpha=0.2, zorder=1, label='95% CI')
    ax.plot(fpr_interp, tprs_mean, color='k', label='AUC: {0:.3f} ({1:.3f}-{2:.3f})'.format(auc_mean, auc_ci[0], auc_ci[1]), linewidth=0.8, zorder=0)
    ax.plot([0, 1], [0, 1], color='crimson', linestyle='--', alpha=1, linewidth=1.5, label='Reference')
    ax.set_xlim([-0.01, 1.00])
    ax.set_ylim([-0.01, 1.01])
    ax.set_ylabel('Sensitivity')
    ax.set_xlabel('1 - Specificity')
    plt.legend(loc="lower right")
    plt.grid(color='k', alpha=0.5)


def nrrd_to_tensor(file):
    """
    This function takes a nrrd file path as input and returns the image as a tensor after normalization
    :param file: A string which specifies the file path of the nrrd file
    :return: A torch tensor that is normalized
    """
    image = sitk.ReadImage(file)
    image = sitk.NormalizeImageFilter().Execute(image)
    image = sitk.GetArrayFromImage(image).astype(np.float64)
    image = torch.from_numpy(image)
    return image


def initialize_CNN2(cnn2_model, modality):
    cnn_model = torch.load("/home/andrewg/PycharmProjects/assignments/predictions/models/{}/CNN/1.pt".format(modality))
    layers = ["conv1.weight", "conv1.bias", "conv2.weight", "conv2.bias", "conv3.weight", "conv3.bias", "conv4.weight",
              "conv4.bias", "conv5.weight", "conv5.bias"]
    state_dict = cnn2_model.state_dict()
    idx = 0
    for name, param in state_dict.items():
        layer = layers[idx]
        transformed_param = cnn_model[layer]
        state_dict[name].copy_(transformed_param)
        idx = idx + 1
        if idx == len(layers):
            return


def cam_visualize_one_image(file):
    seed = 0
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(0)

    cuda_destination = 0
    ngpu = 1
    device = torch.device("cuda:{}".format(cuda_destination) if (torch.cuda.is_available() and ngpu > 0) else "cpu")

    model = CNN2(cuda_destination=cuda_destination)
    model.load_state_dict(torch.load(
        "/home/andrewg/PycharmProjects/assignments/predictions/models/bval/CNN2/46.pt",
        map_location=device))
    model.cuda(cuda_destination)
    model.eval()

    im = sitk.ReadImage(file)
    im = sitk.GetArrayFromImage(im).astype(np.float64)
    plt.imshow(im[1], interpolation="bilinear", cmap="gray")
    plt.axis("off")
    plt.show()
    im = torch.from_numpy(im).cuda()
    cam = model.class_activation_mapping(im.float())
    CNN2.visualize(im, cam)
