import os
import SimpleITK as sitk
from data_helpers import resample_image, crop_from_center


def create_bval_kgh_patients():
    kgh_data_dir = "/home/andrewg/PycharmProjects/assignments/data/KGHData"
    directories = os.listdir(kgh_data_dir)
    bval = dict()
    crops_directory = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/kgh/{}.nrrd"
    for directory in directories:
        sub_directory = "{}/{}".format(kgh_data_dir, directory)
        if not(os.path.isdir(sub_directory)):
            continue
        sub_directory_contents = os.listdir(sub_directory)
        for file in sub_directory_contents:
            if "nrrd" in file:
                bval_nrrd_file = file  # file becomes the nrrd file name at this point
                break
        else:
            continue  # if the for loop never runs
        bval_nrrd_file = "{}/{}".format(sub_directory, bval_nrrd_file)
        try:
            with open("{}/fiducials/{}".format(kgh_data_dir, directory)) as fid_file:
                bval[directory] = resample_image(sitk.ReadImage(bval_nrrd_file), out_spacing=(2, 2, 3))
                for idx, line in enumerate(fid_file):
                    if idx == 3:
                        fiducial = list(map(float, line.split(',')[1:4]))
                        fiducial[0] *= -1
                        fiducial[1] *= -1
                        fiducial = bval[directory].TransformPhysicalPointToIndex(fiducial)
                        bval[directory] = crop_from_center([bval[directory]], [fiducial], 32, 32, 3)[0]
                sitk.WriteImage(bval[directory], crops_directory.format(directory))
        except:
            pass

    return bval


if __name__ == "__main__":
    bval = create_bval_kgh_patients()
    crops_directory = "/home/andrewg/PycharmProjects/assignments/resampled_cropped/kgh/{}.nrrd"
    # for key in bval.keys():
    #     sitk.WriteImage(bval[key], crops_directory.format(key))