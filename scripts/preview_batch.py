"""Generate view-only RGB previews, with compact/output and original/source times.

Uses this project's reader; optional dependency: pip install -e '.[preview]'.
Requires ffmpeg/ffprobe on PATH. Does not resample or edit bag data.
"""
import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess

import cv2
import numpy as np

from bag_worker.bag import Bag,CDR


def decode_rgb(data,kind):
    c=CDR(data);c.header()
    if kind.endswith('/CompressedImage'):
        c.string();image=cv2.imdecode(np.frombuffer(c.byte_array(),np.uint8),cv2.IMREAD_COLOR)
        if image is None:raise ValueError('Cannot decode compressed RGB image')
        return image
    height,width,encoding=c.u32(),c.u32(),c.string()
    c.number('B',1);step=c.u32();raw=c.byte_array()
    channels={'rgb8':3,'bgr8':3,'rgba8':4,'bgra8':4,'mono8':1}.get(encoding)
    if channels is None or height<=0 or width<=0 or step<width*channels or len(raw)<height*step:
        raise ValueError(f'Unsupported or malformed RGB image: {encoding}')
    image=np.frombuffer(raw,np.uint8,count=height*step).reshape(height,step)[:,:width*channels].reshape(height,width,channels)
    code={'rgb8':cv2.COLOR_RGB2BGR,'rgba8':cv2.COLOR_RGBA2BGR,'bgra8':cv2.COLOR_BGRA2BGR,'mono8':cv2.COLOR_GRAY2BGR}.get(encoding)
    return cv2.cvtColor(image,code) if code is not None else image


def preview(source,output,fps=30):
    ffmpeg=shutil.which('ffmpeg');ffprobe=shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:raise ValueError('ffmpeg and ffprobe are required')
    bag=Bag(source);cameras=sorted(n for n in bag.cameras if '/color/' in n)
    if not cameras:raise ValueError('No RGB /color/ camera topics found')
    output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    partial=output.with_name(output.stem+'.partial.mp4')
    if output.exists() or partial.exists():raise ValueError(f'Preview already exists: {output}')
    report=json.loads((bag.root/'trim_report.json').read_text())
    intervals=[(int(x['source_start_ns']),int(x['source_end_ns'])) for x in report['mapping']]
    def source_time(relative_ns):
        compact=bag.start+relative_ns;removed=0
        for a,b in intervals:
            if compact<a-removed:break
            removed+=b-a
        return (compact+removed-int(report['source_start_ns']))/1e9
    width=320*len(cameras);height=264;count=max(1,math.floor(bag.duration*fps));cache={}
    proc=subprocess.Popen([ffmpeg,'-y','-loglevel','error','-f','rawvideo','-pixel_format','bgr24',
        '-video_size',f'{width}x{height}','-framerate',str(fps),'-i','pipe:0','-an','-c:v','libx264',
        '-crf','23','-preset','fast','-pix_fmt','yuv420p','-movflags','+faststart',str(partial)],
        stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for i in range(count):
            seconds=i/fps;canvas=np.zeros((height,width,3),np.uint8)
            for j,topic in enumerate(cameras):
                raw,actual=bag.nearest(topic,seconds);key=(topic,actual)
                if key not in cache:
                    image=decode_rgb(raw,bag.topics[topic]['type']);h,w=image.shape[:2];scale=min(320/w,240/h)
                    cache[key]=cv2.resize(image,(round(w*scale),round(h*scale)))
                image=cache[key];h,w=image.shape[:2];x=j*320+(320-w)//2;y=(240-h)//2
                canvas[y:y+h,x:x+w]=image
            cv2.putText(canvas,f'{bag.name} output {seconds:.3f}s | source {source_time(round(seconds*1e9)):.3f}s',
                        (8,257),cv2.FONT_HERSHEY_SIMPLEX,.55,(255,255,255),1)
            proc.stdin.write(canvas.tobytes())
            if len(cache)>100:cache.clear()
        proc.stdin.close();errors=proc.stderr.read().decode(errors='replace');code=proc.wait()
        if code:raise RuntimeError(errors)
        probe=json.loads(subprocess.check_output([ffprobe,'-v','error','-select_streams','v:0',
            '-show_entries','stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration','-of','json',str(partial)],text=True))
        stream=probe['streams'][0]
        assert stream['codec_name']=='h264' and int(stream['nb_frames'])==count
        bag.check_unchanged();partial.rename(output)
        return {'source':str(bag.root),'preview':str(output),'view_only':True,'fps':fps,
                'frames':count,'bag_duration_s':bag.duration,'encoded_duration_s':float(probe['format']['duration']),
                'cameras':cameras,'deleted_seconds':report['deleted_seconds']}
    except BaseException:
        if proc.poll() is None:proc.kill();proc.wait()
        partial.unlink(missing_ok=True)
        raise
    finally:
        proc.stderr.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path);parser.add_argument('--fps',type=int,default=30)
    args=parser.parse_args()
    if args.fps<=0:parser.error('fps must be positive')
    bags=[args.root] if (args.root/'metadata.yaml').exists() else sorted(p.parent for p in args.root.glob('*/metadata.yaml'))
    if not bags:raise ValueError('No bags found')
    target=args.root/'previews';target.mkdir(exist_ok=True)
    results=[]
    for bag in bags:
        results.append(preview(bag,target/(bag.name+'.mp4'),args.fps))
        (target/'manifest.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(bag.name,'preview ready',flush=True)
