
__all__ = ['get_data','dewarp_imageset','super_res_mcimage','dipy_dti_recon']

from pathlib import Path
from pathlib import PurePath
import os
import pandas as pd
import math
import os.path
from os import path
import pickle
import sys
import numpy as np
import random
import functools
from operator import mul
from scipy.sparse.linalg import svds

from dipy.core.histeq import histeq
import dipy.reconst.dti as dti
from dipy.core.gradients import (gradient_table, gradient_table_from_gradient_strength_bvecs)
from dipy.io.gradients import read_bvals_bvecs
from dipy.segment.mask import median_otsu
from dipy.reconst.dti import fractional_anisotropy, color_fa
import nibabel as nib

import ants
import antspynet
import tensorflow as tf

from multiprocessing import Pool

DATA_PATH = os.path.expanduser('~/.antspymm/')

def get_data( name=None, force_download=False, version=2, target_extension='.csv' ):
    """
    Get ANTsPyMM data filename

    The first time this is called, it will download data to ~/.antspymm.
    After, it will just read data from disk.  The ~/.antspymm may need to
    be periodically deleted in order to ensure data is current.

    Arguments
    ---------
    name : string
        name of data tag to retrieve
        Options:
            - 'all'

    force_download: boolean

    version: version of data to download (integer)

    Returns
    -------
    string
        filepath of selected data

    Example
    -------
    >>> import antspymm
    >>> antspymm.get_data()
    """
    os.makedirs(DATA_PATH, exist_ok=True)

    def download_data( version ):
        url = "https://figshare.com/ndownloader/articles/16912366/versions/" + str(version)
        target_file_name = "16912366.zip"
        target_file_name_path = tf.keras.utils.get_file(target_file_name, url,
            cache_subdir=DATA_PATH, extract = True )
        os.remove( DATA_PATH + target_file_name )

    if force_download:
        download_data( version = version )


    files = []
    for fname in os.listdir(DATA_PATH):
        if ( fname.endswith(target_extension) ) :
            fname = os.path.join(DATA_PATH, fname)
            files.append(fname)

    if len( files ) == 0 :
        download_data( version = version )
        for fname in os.listdir(DATA_PATH):
            if ( fname.endswith(target_extension) ) :
                fname = os.path.join(DATA_PATH, fname)
                files.append(fname)

    if name == 'all':
        return files

    datapath = None

    for fname in os.listdir(DATA_PATH):
        mystem = (Path(fname).resolve().stem)
        mystem = (Path(mystem).resolve().stem)
        mystem = (Path(mystem).resolve().stem)
        if ( name == mystem and fname.endswith(target_extension) ) :
            datapath = os.path.join(DATA_PATH, fname)

    if datapath is None:
        raise ValueError('File doesnt exist. Options: ' , os.listdir(DATA_PATH))
    return datapath




def dewarp_imageset( image_list, iterations=None, padding=0, **kwargs ):
    """
    Dewarp a set of images

    Makes simplifying heuristic decisions about how to transform an image set
    into an unbiased reference space.  Will handle plenty of decisions
    automatically so beware.  Computes an average shape space for the images
    and transforms them to that space.

    Arguments
    ---------
    image_list : list containing antsImages 2D, 3D or 4D

    iterations : number of template building iterations

    padding:  will pad the images by an integer amount to limit edge effects

    kwargs : keyword args
        arguments passed to ants registration - these must be set explicitly

    Returns
    -------
    a dictionary with the mean image and the list of the transformed images

    Example
    -------
    >>> import antspymm
    """
    outlist = []
    avglist = []
    if len(image_list[0].shape) > 3:
        imagetype = 3
        for k in range(len(image_list)):
            avglist.append( ants.slice_image( image_list[k], axis=3, idx=0 ) )
    else:
        imagetype = 0
        avglist=image_list

    if iterations is None:
        iterations = 2

    if padding > 0:
        pw=[]
        for k in range(len(avglist[0].shape)):
            pw.append( padding )
        for k in range(len(avglist)):
            avglist[k] = ants.pad_image( avglist[k], pad_width=pw  )

    btp = ants.build_template( image_list=avglist,
        gradient_step=0.5, blending_weight=0.8,
        iterations=iterations, **kwargs )

    # last - warp all images to this frame
    for k in range(len(image_list)):
        reg=ants.registration( btp, avglist[k], **kwargs )
        # now apply the transformation parameters to all in the time series, if needed
        mywarped = ants.apply_transforms( btp, image_list[k], reg['fwdtransforms'], imagetype=imagetype )
        outlist.append( mywarped )


    return {'dewarpedmean':btp, 'dewarped':outlist }


def super_res_mcimage( image, srmodel, truncation=[0.0001,0.995],
    poly_order=1,
    target_range=[-127.5,127.5],
    verbose=False ):
    """
    Super resolution on a timeseries or multi-channel image

    Arguments
    ---------
    image : an antsImage

    srmodel : a tensorflow fully convolutional model

    truncation :  quantiles at which we truncate intensities to limit impact of outliers e.g. [0.005,0.995]

    poly_order : if not None, will fit a global regression model to map
        intensity back to original histogram space

    target_range : 2-element tuple
        a tuple or array defining the (min, max) of the input image
        (e.g., -127.5, 127.5).  Output images will be scaled back to original
        intensity. This range should match the mapping used in the training
        of the network.

    verbose : boolean

    Returns
    -------
    super resolution version of the image

    Example
    -------
    >>> import antspymm
    """
    idim = image.dimension
    ishape = image.shape
    nTimePoints = ishape[idim - 1]
    mcsr = list()
    counter = 0
    for k in range(nTimePoints):
        mycount = round(k / nTimePoints * 100)
        if verbose and mycount == counter:
            counter = counter + 10
            print(mycount, end="%.", flush=True)
        temp = ants.slice_image( image, axis=idim - 1, idx=k )
        temp = ants.iMath( temp, "TruncateIntensity", truncation[0], truncation[1] )
        mysr = antspynet.apply_super_resolution_model_to_image( temp, srmodel,
            target_range = target_range )
        if k == 0:
            upshape = list()
            for j in range(len(ishape)-1):
                upshape.append( mysr.shape[j] )
            upshape.append( ishape[ idim-1 ] )
            if verbose:
                print("SR will be of voxel size:" + str(upshape) )
        if poly_order is not None:
            bilin = ants.resample_image_to_target( temp, mysr )
            mysr = antspynet.regression_match_image( mysr, bilin, poly_order = poly_order )
        mcsr.append( mysr )

    imageup = ants.resample_image( image, upshape, use_voxels = True )
    if verbose:
        print("Done")

    return ants.list_to_ndimage( imageup, mcsr )



def dipy_dti_recon( image, bvalsfn, bvecsfn, median_radius = 3, numpass = 1, dilate = 2 ):
    """
    Super resolution on a timeseries or multi-channel image

    Arguments
    ---------
    image : an antsImage holding B0 and DWI

    bvalsfn : bvalue filename

    bvecsfn : bvector filename

    median_radius : median_radius from dipy median_otsu function

    numpass : numpass from dipy median_otsu function

    dilate : dilate from dipy median_otsu function

    Returns
    -------
    dictionary holding the tensorfit, MD, FA and RGB images

    Example
    -------
    >>> import antspymm
    """
    bvals, bvecs = read_bvals_bvecs( bvalsfn , bvecsfn   )
    gtab = gradient_table(bvals, bvecs)
    img = image.to_nibabel()
    data = img.get_fdata()
    data3d = data[:,:,:,0] * 1/6
    for x in range(1, 5):
      data3d = data3d + data[:,:,:,0] * 1/6

    maskdata, mask = median_otsu(
        data,
        vol_idx=range(0, 5),
        median_radius = 3,
        numpass = 1,
        autocrop = True,
        dilate = dilate )

    tenmodel = dti.TensorModel(gtab)
    tenfit = tenmodel.fit(maskdata)

    FA = fractional_anisotropy(tenfit.evals)
    FA[np.isnan(FA)] = 0

    MD1 = dti.mean_diffusivity(tenfit.evals)
    FA = np.clip(FA, 0, 1)
    RGB = color_fa(FA, tenfit.evecs)
    return {
        'tensormodel':tenfit,
        'MD':ants.from_nibabel(nib.Nifti1Image(MD1.astype(np.float32), img.affine)),
        'FA':ants.from_nibabel(nib.Nifti1Image(FA.astype(np.float32), img.affine)),
        'RGB': ants.from_nibabel(nib.Nifti1Image(RGB.astype(np.float32), img.affine)) }
