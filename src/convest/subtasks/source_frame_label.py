"""Small deterministic source frame labels without an optional font dependency."""
import numpy as np
_GLYPHS={
'0':['111','101','101','101','111'],'1':['010','110','010','010','111'],
'2':['111','001','111','100','111'],'3':['111','001','111','001','111'],
'4':['101','101','111','001','001'],'5':['111','100','111','001','111'],
'6':['111','100','111','101','111'],'7':['111','001','010','010','010'],
'8':['111','101','111','101','111'],'9':['111','101','111','001','111'],
'F':['111','100','110','100','100'],'V':['101','101','101','101','010'],
'T':['111','010','010','010','010'],'.':['000','000','000','000','010'],
' ':['000']*5}

def stamp(canvas,camera_count,pw,ph,frame,fps):
    for n in range(camera_count):
        x,y=n%2*pw,n//2*ph;canvas[y:y+25,x:x+pw]=0
        for i,ch in enumerate(f'V{n} F{frame} T{frame/fps:.3f}'):
            bits=np.array([[255 if v=='1' else 0 for v in row] for row in _GLYPHS[ch]],dtype=np.uint8)
            bits=np.repeat(np.repeat(bits,3,axis=0),3,axis=1)
            left=x+5+i*12
            if left+9>x+pw:break
            canvas[y+4:y+19,left:left+9]=bits[:,:,None]
    return canvas
