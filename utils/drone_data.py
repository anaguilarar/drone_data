import numpy as np
import xarray
import rasterio
import os
import glob

from utils import data_processing
from utils.plt_functions import plot_multibands_fromxarray
from rasterio.windows import from_bounds

import matplotlib.pyplot as plt

from utils import classification_functions as clf
import pandas as pd
import geopandas as gpd
import rasterio.mask
from utils import gis_functions as gf

import re

def drop_bands(xarraydata, bands):
    for i in bands:
        xarraydata = xarraydata.drop(i)
    
    return xarraydata

def _solve_red_edge_order(listpaths, bands):

    ordered =[]
    for band in bands:
        for src in listpaths:
            if band in src and band not in ordered:
                if "red" in src and "red" == band:
                    if "edge" not in src:
                        ordered.append(src)
                else:
                    ordered.append(src)

    return ordered

def filter_list(list1, list2):
    list_filtered = []
    for strlist2 in list2:
        for strlist1 in list1:
            if strlist2 in strlist1:
                if strlist1 not in list_filtered:
                    list_filtered.append(strlist1)
    
    return list_filtered


def normalized_difference(array1, array2, namask=np.nan):
    if np.logical_not(np.isnan(namask)):
        array1[array1 == namask] = np.nan
        array2[array2 == namask] = np.nan

    return ((array1 - array2) /
            (array1 + array2))


def get_files_paths(path, bands):
    try:

        imgfiles = glob.glob(path + "*.tif")
        
        imgfiles_filtered = filter_list(imgfiles, bands)
        if "edge" in bands:
            imgfiles_filtered = _solve_red_edge_order(imgfiles_filtered, bands)

        return imgfiles_filtered

    except ValueError:
        print("file path doesn't exist")

def calculate_vi_fromxarray(xarraydata, vi='ndvi', expression=None, label=None):

    variable_names = list(xarraydata.keys())
    namask = xarraydata.attrs['nodata']
    # modify expresion finding varnames
    symbolstoremove = ['*','-','+','/',')','(',' ','[',']']
    test = expression
    for c in symbolstoremove:
        test = test.replace(c, '-')

    test = re.sub('\d', '-', test)
    varnames = [i for i in np.unique(np.array(test.split('-'))) if i != '']
    for i, varname in enumerate(varnames):
        if varname in variable_names:
            exp = (['listvar[{}]'.format(i), varname])
            expression = expression.replace(exp[1], exp[0])
        else:
            raise ValueError('there is not a variable named as {}'.format(varname))
    
    listvar = []
    if vi not in variable_names:

        for i, varname in enumerate(varnames):
            if varname in variable_names:
                varvalue = xarraydata[varname].data
                varvalue[varvalue == namask] = np.nan
                listvar.append(varvalue)

        vidata = eval(expression)
            
        if label is None:
            label = vi

        vidata[np.isnan(vidata)] = xarraydata.attrs['nodata']
        vidata[vidata == namask] = np.nan
        xrvidata = xarray.DataArray(vidata)
        xrvidata.name = label
        xrvidata = xrvidata.rename(dict(zip(xrvidata.dims,
                                            list(xarraydata.dims.keys()))))

        xarraydata = xarray.merge([xarraydata, xrvidata])

        xarraydata.attrs['count'] = len(list(xarraydata.keys()))

    else:
        print("the VI {} was calculated before {}".format(vi, variable_names))

    return xarraydata


class DroneData:

    @property
    def variable_names(self):
        return list(self.drone_data.keys())

    def _checkbandstoexport(self, bands):

        if bands == 'all':
            bands = self.variable_names

        elif not isinstance(bands, list):
            bands = [bands]

        bands = [i for i in bands if i in self.variable_names]

        return bands

    def add_layer(self, fn, variable_name):
        with rasterio.open(fn) as src:
            xrimg = xarray.DataArray(src.read(1))

        xrimg.name = variable_name
        xrimg = xrimg.rename({'dim_0': 'y', 'dim_1': 'x'})

        self.drone_data = xarray.merge([self.drone_data, xrimg])

    def data_astable(self):

        npdata2dclean, idsnan = data_processing.from_xarray_to_table(self.drone_data,
                                                                     nodataval=self.drone_data.attrs['nodata'])

        return [npdata2dclean, idsnan]

    def calculate_vi(self, vi='ndvi', expression=None, label=None):
        if vi == 'ndvi':
            if 'nir' in self.variable_names:
                expression = '(nir - red) / (nir + red)' 
            else:
                raise ValueError('It was not possible to calculate ndvi as default, please provide equation')

        elif expression is None:
            raise ValueError('please provide a equation to calculate this index: {}'.format(vi))

        self.drone_data = calculate_vi_fromxarray(self.drone_data, vi, expression, label)

    def rf_classification(self, model, features=None):

        if features is None:
            features = ['blue', 'green', 'red',
                        'r_edge', 'nir', 'ndvi', 'ndvire']

        img_clas = clf.img_rf_classification(self.drone_data, model, features)
        img_clas = xarray.DataArray(img_clas)
        img_clas.name = 'rf_classification'

        self.drone_data = xarray.merge([self.drone_data, img_clas])

    def clusters(self, nclusters=2, method="kmeans", p_sample=10, pcavariance=0.5):
        # preprocess data
        data = self._data
        idsnan = self._nanindex

        if method == "kmeans":
            nsample = int(np.round(data.shape[0] * (p_sample / 100)))
            clusters = clf.kmeans_images(data,
                                         nclusters,
                                         nrndsample=nsample, eigmin=pcavariance)

        climg = data_processing.assign_valuestoimg((clusters['labels'] + 1),
                                                   self.drone_data.dims['y'],
                                                   self.drone_data.dims['x'], idsnan)

        climg = xarray.DataArray(climg)
        climg.name = 'clusters'

        self.drone_data = xarray.merge([self.drone_data, climg])
        self._clusters = clusters

    def extract_usingpoints(self, points,
                            bands=None, crs=None,
                            long_direction=True):
        """

        :param points:
        :param bands:
        :param crs:
        :param long_direction:
        :return:
        """

        if bands is None:
            bands = self.variable_names
        if crs is None:
            crs = self.drone_data.attrs['crs']

        if type(points) == str:
            coords = pd.read_csv(points)

        elif type(points) == list:
            if np.array(points).ndim == 1:
                points = [points]

            coords = pd.DataFrame(points)

        geopoints = gpd.GeoDataFrame(coords,
                                     geometry=gpd.points_from_xy(coords.iloc[:, 0],
                                                                 coords.iloc[:, 1]),
                                     crs=crs)

        return gf.get_data_perpoints(self.drone_data.copy(),
                                     geopoints,
                                     bands,
                                     long=long_direction)

    def tif_toxarray(self, multiband=False, bounds = None):

        riolist = []
        imgindex = 1
        nodata = None
        boundswindow = None
        
        for band, path in zip(self._bands, self._files_path):
            
            with rasterio.open(path) as src:

                tr = src.transform
                nodata = src.nodata
                metadata = src.profile.copy()
                if bounds is not None:
                    #boundswindow = from_bounds(bounds[0],bounds[1],bounds[2],bounds[3], src.transform)
                    #tr = src.window_transform(boundswindow)
                    img, tr = rasterio.mask.mask(src, bounds, crop=True)
                   
                    img = img[(imgindex-1),:,:]
                    img = img.astype(float)
                    img[img == nodata] = np.nan
                    nodata = np.nan

                else:
                    img = src.read(imgindex, window = boundswindow)
                    
                
                metadata.update({
                    'height': img.shape[0],
                    'width': img.shape[1],
                    'transform': tr})

            if img.dtype == 'uint8':
                img = img.astype(float)
                metadata['dtype'] == 'float'


            xrimg = xarray.DataArray(img)
            xrimg.name = band
            riolist.append(xrimg)

            if multiband:
                imgindex += 1

        # update nodata attribute
        metadata['nodata'] = nodata
        metadata['count'] = self._bands

        multi_xarray = xarray.merge(riolist)
        multi_xarray.attrs = metadata

        ## assign coordinates
        #tmpxr = xarray.open_rasterio(self._files_path[0])
        xvalues, yvalues = gf.xy_fromtransform(metadata['transform'], metadata['width'],metadata['height'])

        multi_xarray = multi_xarray.assign_coords(x=xvalues)
        multi_xarray = multi_xarray.assign_coords(y=yvalues)
        
        multi_xarray = multi_xarray.rename({'dim_0': 'y', 'dim_1': 'x'})

        return multi_xarray

    def plot_multiplebands(self, bands, height=20, width=14):
        return plot_multibands_fromxarray(self.drone_data, bands, height, width)

    def plot_singleband(self, band, height=12, width=8):

        # Define a normalization from values -> colors

        datatoplot = self.drone_data[band].data
        datatoplot[datatoplot == self.drone_data.attrs['nodata']] = np.nan
        fig, ax = plt.subplots(figsize=(height, width))

        im = ax.imshow(datatoplot)
        fig.colorbar(im, ax=ax)
        ax.set_axis_off()
        plt.show()

    def multiband_totiff(self, filename, varnames='all'):

        varnames = self._checkbandstoexport(varnames)
        metadata = self.drone_data.attrs

        if filename.endswith('tif'):
            suffix = filename.index('tif')
        else:
            suffix = (len(filename) + 1)

        if len(varnames) > 1:
            metadata['count'] = len(varnames)
            imgstoexport = []
            fname = ""
            for varname in varnames:
                fname = fname + "_" + varname
                imgstoexport.append(self.drone_data[varname].data.copy())

            fn = "{}{}.tif".format(filename[:(suffix - 1)], fname)
            imgstoexport = np.array(imgstoexport)
            with rasterio.open(fn, 'w', **metadata) as dst:
                for id, layer in enumerate(imgstoexport, start=1):
                    dst.write_band(id, layer)

    def to_tiff(self, filename, varnames='all'):

        varnames = self._checkbandstoexport(varnames)
        metadata = self.drone_data.attrs

        if filename.endswith('tif'):
            suffix = filename.index('tif')
        else:
            suffix = (len(filename) + 1)

        if len(varnames) > 0:
            for i, varname in enumerate(varnames):
                imgtoexport = self.drone_data[varname].data.copy()
                fn = "{}_{}.tif".format(filename[:(suffix - 1)], varname)
                with rasterio.open(fn, 'w', **metadata) as dst:
                    dst.write_band(1, imgtoexport)

        else:
            print('check the bands names that you want to export')

    def split_into_tiles(self, polygons=False, **kargs):

        self.tiles_data = gf.split_xarray_data(self.drone_data, polygons=polygons, **kargs)
        print("the image was divided into {} tiles".format(len(self.tiles_data)))


    def __init__(self,
                 inputpath,
                 bands=None,
                 multiband_image=False,
                 table=True,
                 bounds = None):

        if bands is None:
            self._bands = ['red', 'green', 'blue']
        else:
            self._bands = bands

        self._clusters = np.nan
        if not multiband_image:
            self._files_path = get_files_paths(inputpath, self._bands)

        else:
            if inputpath.endswith('.tif'):
                self._files_path = [inputpath for i in range(len(self._bands))]
            else:
                imgfiles = glob.glob(inputpath + "*.tif")[0]
                self._files_path = [imgfiles for i in range(len(self._bands))]


        if len(self._files_path)>0:
            self.drone_data = self.tif_toxarray(multiband_image, bounds=bounds)
        else:
            raise ValueError('Non file path was found')
            

        if table:
            self._data, self._nanindex = self.data_astable()
