from Crop_DL.crop_dl.seeds.utils import getmidleheightcoordinates, euclidean_distance, getmidlewidthcoordinates
from Crop_DL.crop_dl.image_functions import contours_from_image, pad_images
from Crop_DL.crop_dl.plt_utils import plot_segmenimages, random_colors, add_frame_label
from Crop_DL.crop_dl.models.utils import image_to_tensor
import copy
import geopandas as gpd
import pandas as pd
import math

import xarray
import torch
import cv2
import numpy as np
import tqdm

from Crop_DL.crop_dl.dataset_utils import get_boundingboxfromseg

from .drone_data import DroneData
from .data_processing import from_xarray_2array
from .gis_functions import merging_overlaped_polygons

def get_clossest_prediction(image_center, bb_predictions, distance_limit = 30):
    distpos = None
    dist = []
    if bb_predictions is not None:
        if len(bb_predictions)>0:
            for i in range(len(bb_predictions)):
                x1,y1,x2,y2 = bb_predictions[i]
                widthcenter = (x1+x2)//2
                heightcenter = (x1+x2)//2
                dist.append(euclidean_distance([widthcenter,heightcenter],image_center))
            
            if np.min(dist)<distance_limit:
                distpos = np.where(np.array(dist) == np.min(dist))[0][0]

    return distpos, dist

def _apply_mask(image, mask, color, alpha=0.5):
    """Apply the given mask to the image.
    """
    for c in range(3):
        image[:, :, c] = np.where(mask == 1,
                                  image[:, :, c] *
                                  (1 - alpha) + alpha * color[c] * 255,
                                  image[:, :, c])
    return image

class SegmentationUAVData(DroneData):
    
    
    def __init__(self,
                 model,
                 imagepath = None,
                 uavimagery = None,
                 inputsize = (512, 512),
                 tiles_size = (256, 256),
                 tiles_overlap = 0,
                 device = None,
                 multiband_image=True,
                 rgbbands = ["red","green","blue"],
                 spatial_boundary = None) -> None:
        
        import xarray
        self.layer_predictions = {}
        
        if isinstance(uavimagery,xarray.Dataset):
            self.drone_data = uavimagery
        elif imagepath is not None:
            super().__init__(imagepath,multiband_image=multiband_image, bounds = spatial_boundary)
        
            if tiles_size is not None:
                self.split_into_tiles(width = tiles_size[0], height = tiles_size[1], overlap = tiles_overlap) 
            #self.uav_imagery = .drone_data
            
        self.input_model_size = inputsize
        self.model = model
        self._frames_colors = None
        self.tiles_size = tiles_size
        self.rgbbands = rgbbands
        if device is None:
            self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device
    
    def _original_size(self, orig_size = None):
        msksc = [0]* len(list(self.msks))
        
        bbsc = [0]* len(list(self.bbs))
        
        for i in range(len(self.msks)):
            msksc[i] = cv2.resize(self.msks[i], 
                                  [orig_size[1],orig_size[0]], 
                                  interpolation = cv2.INTER_AREA)  
            if len(bbsc)>0:
                bbsc[i] = get_boundingboxfromseg(msksc[i])
            #else:
            #    bbsc[i] = []
        
        self.msks = np.array(msksc)
        self.bbs = np.array(bbsc)
        
             
    def _filter_byscore(self, threshold):
        
        pred = self.predictions[0] 
        onlythesepos = np.where(
            pred['scores'].to('cpu').detach().numpy()>threshold)
        
        msks = pred['masks'].mul(255).byte().cpu().numpy()[onlythesepos, 0].squeeze()
        bbs = pred['boxes'].cpu().detach().numpy()[onlythesepos]
        
        if msks.shape[0] == 0:
            msks = np.zeros(self.input_model_size)
            
        if len(msks.shape)==2:
            msks = np.expand_dims(msks,0)
                
        return msks, bbs
    
    def uav_image_prediction(self,uavimage = None, threshold = 0.75, segment_threshold = 180):
        
        if uavimage is None:
            uavimage = self.drone_data.copy()
        
        self.msks = None
        self.bbs = None
        self.scores = None
        
        if np.nansum(uavimage.to_array().values)>0:
            img = from_xarray_2array(uavimage, self.rgbbands , True)
            imgsize = img.copy().shape[1:]
            
            self._img = img
            
            imgtensor = image_to_tensor(img.copy(), self.input_model_size)
            imgtensor.shape
            self.model.eval()
            with torch.no_grad():
                prediction = self.model([imgtensor.to(self.device)])
                
            self.predictions = prediction
            self.idimg = id
            self._imgastensor = imgtensor
            self.scores  = self.predictions[0]['scores'].to('cpu').detach().numpy()
            
            pred = self._filter_byscore(threshold)
            self.bbs = pred[1]
            self.msks = pred[0]
            
            for i in range(len(self.msks)):
                self.msks[i][self.msks[i]<segment_threshold] = 0
            
            self._original_size(imgsize)
            
           
        return {'masks': self.msks, 
                'bbs':self.bbs, 
                'scores':self.scores}
    
    def predict_single_tile(self, id_tile, threshold = 0.75, segment_threshold = 180):
        
        tiledata = self.tiles_data(id_tile)
        height, width = len(tiledata.y),len(tiledata.x)
        ratio = np.min([height/width,width/height])

        self._currenttile = id_tile

        predictions = {'masks': None, 
                'bbs':None, 
                'scores':None}
        if ratio>0.9:
            predictions = self.uav_image_prediction(tiledata, threshold = threshold, 
                                      segment_threshold = segment_threshold)
            
        self.layer_predictions[str(id_tile)] = {'masks': self.msks,
                                                    'bbs':self.bbs}
        return predictions
        
    def plot_prediction(self, **kwargs):
        
        plotimage = None
        
        
        if self.bbs is not None:
            
            if self.bbs is not None:
                bbs= self.bbs.astype(np.uint16)
                if self._frames_colors is None:
                    self._frames_colors = random_colors(len(self.bbs))
            #if id_tile is None:
            #    id_tile = self._currenttile
            #img = from_xarray_2array(self.tiles_data(self._currenttile), ["red","green","blue"], True)
            img = copy.deepcopy(self._img)
            if self.msks.shape[0] == 0:
                msksone = np.zeros(img.shape[:2])
            else:
                msksone = np.max(self.msks, axis = 0)
                
            plotimage =  plot_segmenimages(img.swapaxes(0,1).swapaxes(
                1,2).astype(np.uint8),
                  np.max(np.stack(
                    self.msks), axis = 0)*255, 
                        boxes=self.bbs, 
                        bbtype = 'xminyminxmaxymax',
                        default_color = self._frames_colors,
                        inverrgbtorder=False)
            
        print(self._frames_colors)
        return plotimage
    
    def tile_predictions_asgpd(self, id_tile, **kwargs):

        tile_predictions = self.predict_single_tile(id_tile, **kwargs)
        output = None
        if tile_predictions['bbs'] is not None:
            tileimg = self.tiles_data(self._currenttile)
            crs_system  = tileimg.attrs['crs']
            polsshp_list= []
            for i in range(len(tile_predictions['bbs'])):
                from drone_data.utils.gis_functions import from_bbxarray_2polygon
                
                bb_polygon = from_bbxarray_2polygon(tile_predictions['bbs'][i], tileimg)

                pred_score = np.round(tile_predictions['scores'][i] * 100, 3)

                gdr = gpd.GeoDataFrame({'pred': [i],
                                        'score': [pred_score],
                                        'geometry': bb_polygon},
                                    crs=crs_system)

                polsshp_list.append(gdr)
            if len(polsshp_list):
                output = pd.concat(polsshp_list, ignore_index=True)

        return output
    
    
    def detect_oi_in_uavimage(self, overlap = None, aoi_limit = 0.5, 
                              onlythesetiles = None,threshold = 0.8, **kwargs):
        """
        a function to detect opbect of interest in a RGB UAV image

        parameters:
        ------
        imgpath: str:
        """
        overlap = [0] if overlap is None else overlap
        peroverlap = []
        for spl in overlap:
            allpols_pred = []
            print(f"split overlap {spl}")
            self.split_into_tiles(width = self.tiles_size[0], height = self.tiles_size[1], overlap = spl) 
            
            if onlythesetiles is not None:
                tileslist =  onlythesetiles
            else:
                tileslist =  list(range(len(self._tiles_pols)))

            for i in tqdm.tqdm(tileslist):
                
                #tile_predictions = self.predict_single_tile(i, threshold=threshold, **kwargs)
                
                bbasgeodata = self.tile_predictions_asgpd(i, threshold=threshold, **kwargs)
                
                if bbasgeodata is not None:
                    bbasgeodata['tile']= [i for j in range(bbasgeodata.shape[0])]
                    allpols_pred.append(bbasgeodata)
            
            allpols_pred_gpd = pd.concat(allpols_pred)
            allpols_pred_gpd['id'] = [i for i in range(allpols_pred_gpd.shape[0])]
            total_objects = merging_overlaped_polygons(allpols_pred_gpd, aoi_limit = aoi_limit)
            peroverlap.append(pd.concat(total_objects))

        allpols_pred_gpd = pd.concat(peroverlap)
        allpols_pred_gpd['id'] = [i for i in range(allpols_pred_gpd.shape[0])]
        #allpols_pred_gpd.to_file("results/alltest_id.shp")
        print("{} polygons were detected".format(allpols_pred_gpd.shape[0]))
        
        total_objects = merging_overlaped_polygons(allpols_pred_gpd, aoi_limit = aoi_limit)
        total_objects = pd.concat(total_objects) 
        print("{} boundary boxes were detected".format(total_objects.shape[0]))
        
        return total_objects
    
    def calculate_onecc_metrics(self, cc_id, padding = 20, hull = True):

        #imageres = self._imgastensor.mul(255).permute(1, 2, 0).byte().numpy()

        maskimage = self._clip_image(self.msks[cc_id], self.bbs[cc_id], padding = padding)
        wrapped_box = self._find_contours(maskimage, hull=hull)
        pheightu, pheigthb, pwidthu, pwidthb = self._get_heights_and_widths(wrapped_box)
        d1 = euclidean_distance(pheightu, pheigthb)
        d2 = euclidean_distance(pwidthu, pwidthb)
        #distper = np.unique([euclidean_distance(wrapped_box[i],wrapped_box[i+1]) for i in range(len(wrapped_box)-1) ])
        ## with this statement there is an assumption that the rice width is always lower than height
        larger = d1 if d1>d2 else d2
        shorter = d1 if d1<d2 else d2
        msksones = maskimage.copy()
        msksones[msksones>0] = 1
        
        area = np.sum(msksones*1.)

        return {
            'seed_id':[cc_id],'height': [larger], 
                'width': [shorter], 'area': [area]}
        
        ## metrics from seeds
    def plot_individual_cc(self, cc_id,**kwargs):
        
        return self._add_metriclines_to_single_detection(cc_id, **kwargs)

    def _add_metriclines_to_single_detection(self, 
                                             cc_id, 
                    addlines = True, addlabel = True,
                    padding = 30,
                    mask_image = False,
                    sizefactorred = 250,
                    heightframefactor = .15,
                    widthframefactor = .3,
                    textthickness = 1):
        
        import copy
        print(self._frames_colors)
        if self._frames_colors is None:
            self._frames_colors = random_colors(len(self.bbs))
            
        col = self._frames_colors[cc_id]
        
        imageres = self._img.copy()
        if imageres.shape[0] == 3:
            imageres = imageres.swapaxes(0,1).swapaxes(1,2)
            
        imgclipped = copy.deepcopy(self._clip_image(imageres, self.bbs[cc_id], 
                                                    padding = padding,paddingwithzeros = False))
        maskimage = copy.deepcopy(self._clip_image(self.msks[cc_id], 
                                                               self.bbs[cc_id], 
                                                               padding = padding,
                                                               paddingwithzeros = False))
        
        #maskimage = self._clip_image(self.msks[cc_id], self.bbs[cc_id], padding = padding)
        #wrapped_box = self._find_contours(maskimage)
        

        msksones = maskimage.copy()
        
        msksones[msksones<150] = 0
        msksones[msksones>=150] = 1
        #msksones[msksones>0] = 1
        
        
        if mask_image:

            newimg = cv2.bitwise_and(imgclipped.astype(np.uint8),img,
                                     mask = msksones)
        else:
            newimg = np.array(imgclipped)
        
        img = _apply_mask(newimg, (msksones).astype(np.uint8), col, alpha=0.2)
        #img = newimg
        
        linecolor = list((np.array(col)*255).astype(np.uint8))
        m = np.ascontiguousarray(img, dtype=np.uint8)
        if addlines:
            m = cv2.drawContours(m,[self._find_contours(maskimage, hull = True)],0,[int(i) for i in linecolor],1)
            pheightu, pheigthb, pwidthu, pwidthb = self._get_heights_and_widths(
                self._find_contours(maskimage, hull = True))
            m = cv2.line(m, pheightu, pheigthb, (0,0,0), 1)
            m = cv2.line(m, pwidthu, pwidthb, (0,0,0), 1)
        
        if addlabel:
            
            x1,y1,x2,y2 = get_boundingboxfromseg(maskimage)

            m = add_frame_label(m,
                    str(cc_id),
                    [int(x1),int(y1),int(x2),int(y2)],[
                int(i*255) for i in col],
                    sizefactorred = sizefactorred,
                    heightframefactor = heightframefactor,
                    widthframefactor = widthframefactor,
                    textthickness = textthickness)
            
        return m,maskimage
    
        
    @staticmethod
    def _get_heights_and_widths(maskcontours):

        p1,p2,p3,p4=maskcontours
        alpharad=math.acos((p2[0] - p1[0])/euclidean_distance(p1,p2))

        pheightu=getmidleheightcoordinates(p2,p3,alpharad)
        pheigthb=getmidleheightcoordinates(p1,p4,alpharad)
        pwidthu=getmidlewidthcoordinates(p4,p3,alpharad)
        pwidthb=getmidlewidthcoordinates(p1,p2,alpharad)

        return pheightu, pheigthb, pwidthu, pwidthb
    
    @staticmethod 
    def _clip_image(image, bounding_box, bbtype = 'xminyminxmaxymax', padding = None, 
                    paddingwithzeros =True):
        
        if bbtype == 'xminyminxmaxymax':
            x1,y1,x2,y2 = bounding_box
            x1,y1,x2,y2 = int(x1),int(y1),int(x2),int(y2)
            
        if padding:
            if paddingwithzeros:
                imgclipped = image[
                y1:y2,x1:x2] 
                
                imgclipped = pad_images(imgclipped, padding_factor = padding)
            else:
                height = abs(y1-y2)
                width = abs(x1-x2)
                zoom_factor = padding / 100 if padding > 1 else padding
                new_height, new_width = height + int(height * zoom_factor), width + int(width * zoom_factor)  
                pad_height1, pad_width1 = abs(new_height - height) // 2, abs(new_width - width) //2
                newy1 = 0 if (y1 - pad_height1)<0 else (y1 - pad_height1)
                newx1 = 0 if (x1 - pad_width1)<0 else (x1 - pad_width1)
                imgclipped = image[newy1:newy1+(height+pad_height1*2), 
                                   newx1:newx1+(width+pad_width1*2)] 
        
        
        return imgclipped

    @staticmethod 
    def _find_contours(image, hull = False):
        maskimage = image.copy()
        #imgmas = (maskimage*255).astype(np.uint8)
        contours = contours_from_image(maskimage)
        if hull:
            firstcontour = cv2.convexHull(contours[0])
        else:
            firstcontour = contours[0]
            
        rect = cv2.minAreaRect(firstcontour)
        
        box = cv2.boxPoints(rect)
        box = np.int0(box)
        return box