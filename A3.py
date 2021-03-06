import os
import SimpleITK as sitk
import torch.nn as nn
import torch.utils.data
from torch.utils.data import DataLoader
import pandas as pd
import pickle as pk
from data_helpers import ProstateImages, k_fold_cross_validation
from models import CNN, CNN2


def read_cropped_images(modality):
    """
    This function reads in images of a certain modality and stores them in the dictionary
    cropped_images
    :param modality: ex. t2, adc, bval, etc.
    :return: A dictionary where the first key is a patient number and the second key is the
    fiducial number (with 0 indexing)
    """

    cropped_images = {}
    destination = \
        r"/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/{}".format(modality)
    start_position_patient_number = -11
    end_position_patient_number = -8
    fiducial_number_pos = -6
    image_dir = os.listdir(destination)
    for image_file_name in image_dir:
        image = sitk.ReadImage("{}/{}".format(destination, image_file_name))
        patient_number = int(image_file_name[start_position_patient_number:
                                             end_position_patient_number + 1])
        fiducial_number = int(image_file_name[fiducial_number_pos])

        if patient_number not in cropped_images.keys():
            cropped_images[patient_number] = {}

        cropped_images[patient_number][fiducial_number] = image

    return cropped_images


def test_predictions(dataloader, model, softmax=False):
    """
    This function runs the model on the batches in the test set and returns a dataframe with ProxID, fid, and ClinSig
    columns. The predictions x <- ClinSig, 0 <= x <= 1, x <- R.
    :param dataloader: The data loader with the test batches
    :param model: The trained pytorch model
    :return: A dataframe as described above
    """

    model.eval()
    test_file = r"/home/andrewg/PycharmProjects/assignments/ProstateX-TestLesionInformation/ProstateX-Findings-Test.csv"
    predictions = pd.read_csv(test_file)
    predictions.insert(4, "ClinSig", 0)
    predictions = predictions.drop(["pos", "zone"], axis=1)
    end_batch = 0

    for idx, batch in enumerate(dataloader):
        outputs = model(batch["image"])
        if softmax:
            outputs = torch.tensor([tup[1] for tup in outputs])
        start_batch = end_batch
        end_batch = start_batch + len(outputs)
        predictions["ClinSig"].iloc[start_batch: end_batch] = outputs.flatten().tolist()
    return predictions


if __name__ == "__main__":
    seed = 0
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    # Define hyper-parameters
    cuda_destination = 1
    batch_size_train = 100
    batch_size_val = 50
    batch_size_test = 50
    k_low, k_high = 0, 5
    epochs = 20
    lr = 0.00001
    final_lr = 0.01
    softmax = False
    if softmax:
        loss_function = nn.CrossEntropyLoss().cuda(cuda_destination)
        model = CNN2
        model_type = "CNN2"
    else:
        loss_function = nn.BCELoss().cuda(cuda_destination)
        model = CNN
        model_type = "CNN"

    ngpu = 1
    device = torch.device("cuda:{}".format(cuda_destination) if (torch.cuda.is_available() and ngpu > 0) else "cpu")
    modality = "adc"
    image_folder_contents = os.listdir("/home/andrewg/PycharmProjects/assignments/resampled_cropped/train/{}".format(
                                                                                                            modality))

    with open("/home/andrewg/PycharmProjects/assignments/train_key_mappings2.pkl", "rb") as f:
        train_key_mappings = pk.load(f)
    with open("/home/andrewg/PycharmProjects/assignments/fold_key_mappings2.pkl", "rb") as f:
        fold_key_mappings = pk.load(f)

    p_images_train = ProstateImages(modality=modality, train=True, device=device, normalize_strategy=1,
                                    mapping=train_key_mappings)

    p_images_validation = ProstateImages(modality=modality, train=True, device=device, normalize_strategy=1,
                                         mapping=fold_key_mappings)

    dataloader_train = DataLoader(p_images_train, batch_size=batch_size_train, shuffle=True)
    dataloader_val = DataLoader(p_images_validation, batch_size=batch_size_val)

    models_and_scores = k_fold_cross_validation(model, k_low=k_low, k_high=k_high, train_data=(p_images_train,
                                                dataloader_train), val_data=(p_images_validation, dataloader_val),
                                                epochs=epochs, loss_function=loss_function, lr=lr, softmax=softmax,
                                                show=True, final_lr=final_lr, device=device)
    p_images_test = ProstateImages(modality=modality, train=False, device=device)
    dataloader_test = DataLoader(p_images_test, batch_size=batch_size_test, shuffle=False)

    # model = CNN(cuda_destination=cuda_destination)
    # model.load_state_dict(torch.load("/home/andrewg/PycharmProjects/assignments/predictions/models/1.pt",
    #                                  map_location=device))
    # model.cuda(cuda_destination)
    model = models_and_scores[0][0]

    model_dir = "/home/andrewg/PycharmProjects/assignments/predictions/models/{}/{}".format(modality, model_type)
    predictions_dir = "/home/andrewg/PycharmProjects/assignments/predictions/prediction_files"
    sort_key = lambda file_name: int(file_name.split('.')[0])
    model_files = [f for f in os.listdir(model_dir) if f[0] in '123456789']
    results_files = [f for f in os.listdir(predictions_dir) if f[0] in '123456789']
    model_files = sorted(model_files, key=sort_key)
    results_files = sorted(results_files, key=sort_key)

    if model_files:
        next_model, _ = model_files[-1].split('.')
        next_model = "{}.pt".format(int(next_model) + 1)
    else:
        next_model = "1.pt"

    if results_files:
        next_result, _ = results_files[-1].split('.')
        next_result = "{}.csv".format(int(next_result) + 1)
    else:
        next_result = "1.csv"

    results = test_predictions(dataloader_test, model, softmax=softmax)
    torch.save(models_and_scores[0][0].state_dict(), "{}/{}".format(model_dir, next_model))
    # unsure_images_ids = results.query("0.45 <= ClinSig <= 0.55").index
    # results.ClinSig.iloc[unsure_images_ids] = results.ClinSig.iloc[unsure_images_ids].apply(lambda x: 0.3)
    results.to_csv("{}/{}".format(predictions_dir, next_result))
