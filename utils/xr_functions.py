
from ast import Raise
import numpy as np
import tqdm
import pickle
import xarray
from shapely.geometry import Polygon
import richdem as rd


from .gis_functions import get_tiles, resize_3dxarray
from .gis_functions import resample_xarray



def stack_as4dxarray(xarraylist, 
                     sizemethod='max',
                     axis_name = 'date', 
                     valuesaxis_names = None,
                     new_dimpos = 0,
                     resizeinter_method = 'nearest'):
    """
    this function is used to stack multiple xarray along a time axis 
    the new xarray value will have dimension {T x C x H x W}

    Parameters:
    ---------
    xarraylist: list
        list of xarray
    sizemethod: str, optional
        each xarray will be resized to a common size, the choosen size will be the maximun value in x and y or the average
        {'max' , 'mean'} default: 'max'
    axis_name: str, optional
        dimension name assigned to the 3 dimensional axis, default 'date' 
    valuesaxis_name: list, optional
        values for the 3 dimensional axis
    resizeinter_method:
        which resize method will be used to interpolate the grid, this uses cv2
         ({"bilinear", "nearest", "bicubic"}, default: "nearest")
    
    Return:
    ----------
    xarray of dimensions {T x C x H x W}

    """
    if type(xarraylist) is not list:
        raise ValueError('Only list xarray are allowed')

    ydim, xdim = list(xarraylist[0].dims.keys())

    coordsvals = [[xarraylist[i].dims[xdim],
                   xarraylist[i].dims[ydim]] for i in range(len(xarraylist))]

    if sizemethod == 'max':
        sizex, sizexy = np.max(coordsvals, axis=0).astype(np.uint)
    elif sizemethod == 'mean':
        sizex, sizexy = np.mean(coordsvals, axis=0).astype(np.uint)

    # transform each multiband xarray to a standar dims size

    xarrayref = resize_3dxarray(xarraylist[0], [sizex, sizexy], interpolation=resizeinter_method, blur = False)
    xarrayref = xarrayref.assign_coords({axis_name : valuesaxis_names[0]})
    xarrayref = xarrayref.expand_dims(dim = {axis_name:1}, axis = new_dimpos)

    resmethod = 'linear' if resizeinter_method != 'nearest' else resizeinter_method

    xarrayref = adding_newxarray(xarrayref, 
                     xarraylist[1:],
                     valuesaxis_names=valuesaxis_names[1:], method = resmethod)

    xarrayref.attrs['count'] = len(list(xarrayref.keys()))
    
    return xarrayref


def adding_newxarray(xarray_ref,
                     new_xarray,
                     axis_name = 'date',
                     valuesaxis_names = None,
                     resample_method = 'nearest'):
    """
    function to add new data to a previous multitemporal imagery
    
    Parameters
    ----------
    xarray_ref : xarray.core.dataset.Dataset
        multitemporal data that will be used as reference

    new_xarray: xarray.core.dataset.Dataset
        new 2d data that will be added to xarray used as reference
    axis_name: str, optional
        dimension name assigned to the 3 dimensional axis, default 'date' 
    valuesaxis_name: list, optional
        values for the 3 dimensional axis

    Return
    ----------
    xarray of 3 dimensions
    
    """
    if type(xarray_ref) is not xarray.core.dataset.Dataset:
        raise ValueError('Only xarray is allowed')
    
    new_xarray = new_xarray if type(new_xarray) is list else [new_xarray]
    if valuesaxis_names is None:
        valuesaxis_names = [i for i in range(len(new_xarray))]
    
    # find axis position
    dimpos = [i for i,dim in enumerate(xarray_ref.dims.keys()) if dim == axis_name][0]

    # transform each multiband xarray to a standar dims size
    singlexarrayref = xarray_ref.isel({axis_name:0})
    listdatesarray = []
    for i in range(len(new_xarray)):
        arrayresizzed = resample_xarray(new_xarray[i], singlexarrayref, method = resample_method)

        arrayresizzed = arrayresizzed.assign_coords({axis_name : valuesaxis_names[i]})
        arrayresizzed = arrayresizzed.expand_dims(dim = {axis_name:1}, axis = dimpos)
        listdatesarray.append(arrayresizzed)

    mltxarray = xarray.concat(listdatesarray, dim=axis_name)    
    # concatenate with previous results
    xarrayupdated = xarray.concat([xarray_ref,mltxarray], dim=axis_name)
    xarrayupdated.attrs['count'] = len(list(xarrayupdated.keys()))
    return xarrayupdated


def calculate_terrain_layers(xr_data, dem_varname = 'z',attrib = 'slope_degrees', name4d = 'date'):
    """
    Function to calculate terrain attributes from dem layer
    """
    if dem_varname not in list(xr_data.keys()):
        raise ValueError('there is not variable called {dem_varname} in the xarray')
    
    terrainattrslist = []
    #name4d = list(xrdata.dims.keys())[0]
    if len(xr_data.dims.keys())>2:
        for dateoi in range(len(xr_data[name4d])):
            datadem = xr_data[dem_varname].isel({name4d:dateoi}).copy()
            datadem = rd.rdarray(datadem, no_data=0)
            terrvalues = rd.TerrainAttribute(datadem,attrib= attrib)
            terrainattrslist.append(terrvalues)
                    
        xrimg = xarray.DataArray(terrainattrslist)
        vars = list(xr_data.dims.keys())
        
        vars = [vars[i] for i in range(len(vars)) if i != vars.index(name4d)]

        xrimg.name = attrib
        xrimg = xrimg.rename({'dim_0': name4d, 
                            'dim_1': vars[0],
                            'dim_2': vars[1]})
    else:
        datadem = xarray.DataArray(xr_data[dem_varname].copy())
        datadem = rd.rdarray(datadem, no_data=0)
        terrvalues = rd.TerrainAttribute(datadem,attrib= attrib)

        vars = list(xr_data.dims.keys())
        xrimg.name = attrib
        xrimg = xrimg.rename({'dim_0': vars[0], 
                            'dim_1': vars[1]})

    return xr_data.merge(xrimg)


def split_xarray_data(xr_data, polygons=True, **kargs):
    """
    Function to split the xarray data into tiles of x y y pixels
    
    :param xr_data: xarray data with x and y coordinates names
    :param polygons: export polygons
    :param kargs:
         nrows:
         ncols:
         width:
         height:
        :param overlap: [0.0 - 1]
        :return:
    :return:
    """
    xarrayall = xr_data.copy()
    m = get_tiles(xarrayall.attrs, **kargs)

    boxes = []
    orgnizedwidnowslist = []
    i = 0
    for window, transform in m:

        #xrmasked = crop_using_windowslice(xarrayall, window, transform)
        #imgslist.append(xrmasked)
        orgnizedwidnowslist.append((window,transform))
        if polygons:
            coords = window.toranges()
            xcoords = coords[1]
            ycoords = coords[0]
            boxes.append((i, Polygon([(xcoords[0], ycoords[0]), (xcoords[1], ycoords[0]),
                                      (xcoords[1], ycoords[1]), (xcoords[0], ycoords[1])])))
        i += 1
    if polygons:
        output = [m, boxes]
    #else:
    #    output = imgslist

    else:
        output = orgnizedwidnowslist
    return output


def get_xyshapes_from_picklelistxarray(fns_list, 
                                      name4d = 'date', 
                                      dateref = 26):
    """
    get nin max values from a list of xarray

    ----------
    Parameters
    fns_list : list of pickle filenames
    ----------
    Returns
    xshapes: a list that contains the x sizes
    yshapes: a list that  contains the y sizes
    """
    if not (type(fns_list) == list):
        raise ValueError('fns_list must be a list of pickle')


    xshapes = []
    yshapes = []

    for idpol in tqdm.tqdm(range(len(fns_list))):
        with open(fns_list[idpol],"rb") as fn:
            xrdata = pickle.load(fn)
            
        if len(xrdata.dims.keys())>2:
            if dateref is not None:
                xrdata = xrdata.isel({name4d:dateref})
        xshapes.append(xrdata.dims[list(xrdata.dims.keys())[1]])
        yshapes.append(xrdata.dims[list(xrdata.dims.keys())[0]])

    return xshapes, yshapes


def get_minmax_from_picklelistxarray(fns_list, 
                                      name4d = 'date', 
                                      bands = None, 
                                      dateref = 26):
    """
    get nin max values from a list of xarray

    ----------
    Parameters
    fns_list : list of pickle filenames
    ----------
    Returns
    min_dict: a dictionary which contains the minimum values per band
    max_dict: a dictionary which contains the maximum values per band
    """
    if not (type(fns_list) == list):
        raise ValueError('fns_list must be a list of pickle')

    with open(fns_list[0],"rb") as fn:
        xrdata = pickle.load(fn)

    if bands is None:
        bands = list(xrdata.keys())
    

    min_dict = dict(zip(bands, [9999]*len(bands)))
    max_dict = dict(zip(bands, [-9999]*len(bands)))

    for idpol in tqdm.tqdm(range(len(fns_list))):
        with open(fns_list[idpol],"rb") as fn:
            xrdata = pickle.load(fn)
            
        if len(xrdata.dims.keys())>2:
            if dateref is not None:
                xrdata = xrdata.isel({name4d:dateref})
        for varname in list(bands):
            mindict = min_dict[varname]
            maxdict = max_dict[varname]
            minval = np.nanmin(xrdata[varname].values)
            maxval = np.nanmax(xrdata[varname].values)

            max_dict[varname] = maxval if maxdict< maxval else maxdict
            min_dict[varname] = minval if mindict> minval else mindict

    return min_dict, max_dict


def get_meanstd_fromlistxarray(xrdatalist, name4d = 'date'):
    """
    get nin max values from a list of xarray

    ----------
    Parameters
    xrdatalist : list of xarrays
    ----------
    Returns
    min_dict: a dictionary which contains the minimum values per band
    max_dict: a dictionary which contains the maximum values per band
    """
    if not (type(xrdatalist) == list):
        raise ValueError('xrdatalist must be a list of xarray')

    mean_dict = dict(zip(list(xrdatalist[0].keys()), [9999]*len(list(xrdatalist[0].keys()))))
    std_dict = dict(zip(list(xrdatalist[0].keys()), [-9999]*len(list(xrdatalist[0].keys()))))
    for varname in list(xrdatalist[0].keys()):
        datapervar = []
        for idpol in range(len(xrdatalist)):
            
        
            datapervar.append(xrdatalist[idpol][varname].to_numpy())

        mean_dict[varname] = np.nanmean(datapervar)
        std_dict[varname] = np.nanstd(datapervar)

    return mean_dict, std_dict



def get_minmax_fromlistxarray(xrdatalist, name4d = 'date'):
    """
    get nin max values from a list of xarray

    ----------
    Parameters
    xrdatalist : list of xarrays
    ----------
    Returns
    min_dict: a dictionary which contains the minimum values per band
    max_dict: a dictionary which contains the maximum values per band
    """
    if not (type(xrdatalist) == list):
        raise ValueError('xrdatalist must be a list of xarray')

    min_dict = dict(zip(list(xrdatalist[0].keys()), [9999]*len(list(xrdatalist[0].keys()))))
    max_dict = dict(zip(list(xrdatalist[0].keys()), [-9999]*len(list(xrdatalist[0].keys()))))

    for idpol in range(len(xrdatalist)):
        for varname in list(xrdatalist[idpol].keys()):
            minval = min_dict[varname]
            maxval = max_dict[varname]
            if len(xrdatalist[idpol].dims.keys())>2:      
                for i in range(xrdatalist[idpol].dims[name4d]):
                    refvalue = xrdatalist[idpol][varname].isel({name4d:i}).values
                    if minval>np.nanmin(refvalue):
                        min_dict[varname] = np.nanmin(refvalue)
                        minval = np.nanmin(refvalue)
                    if maxval<np.nanmax(refvalue):
                        max_dict[varname] = np.nanmax(refvalue)
                        maxval = np.nanmax(refvalue)
            else:
                refvalue = xrdatalist[idpol][varname].values
                if minval>np.nanmin(refvalue):
                        min_dict[varname] = np.nanmin(refvalue)
                        minval = np.nanmin(refvalue)
                if maxval<np.nanmax(refvalue):
                        max_dict[varname] = np.nanmax(refvalue)
                        maxval = np.nanmax(refvalue)

    return min_dict, max_dict



