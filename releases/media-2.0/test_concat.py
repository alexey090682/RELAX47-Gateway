from test_pipeline import m, stay, VOICES
import tempfile, subprocess, json
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp);q=m.StayMediaQueue(root);q.initialize_media();p=m.source_profile(stay(),VOICES);manifest={}
 for mode,color in zip(m.PARTS,['red','green','blue']):
  payload={'mode':mode,'source_signature':m.canonical(p),'scenes':[{'message':mode}]}
  out,status=q.paths(payload);out.parent.mkdir(parents=True)
  subprocess.run(['ffmpeg','-loglevel','error','-y','-f','lavfi','-i',f'color=c={color}:s=320x180:r=25','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','0.4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(out)],check=True)
  manifest[mode]={'state':'ready','mode':mode,'render_job_id':m.job_identity(payload),'source_signature':m.canonical(p),'section_count':1,'scene_count':1}
 result=m.concat_media(root,p,manifest)
 assert result['validation']=='passed' and result['scene_count']==3
 assert 1.1 < result['duration'] < 1.5,result
 print('Real FFmpeg: three parts remuxed, streams/duration verified, no re-encode')
