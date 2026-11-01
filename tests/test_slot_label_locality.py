import copy
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont
from services.evidence_label_layout import render_labels, source_patch_pnginfo, restore_source_patches
from services.template_evidence import _evidence_question_label_font

# October user decision supersedes always-stacked / borderless September labels.
# Keep locality, alpha, foreground, replay and rejection assertions below.

def test_slot_labels_never_jump_across_other_answers():
 crop=np.full((400,1000,3),255,np.uint8)
 boxes=[[80,100,210,180],[300,100,440,180],[520,100,660,180],[740,100,900,180]]
 for x1,y1,x2,y2 in boxes:crop[y1:y2,x1:x2]=180
 targets=[{'question_number':'2','slot_id':f'2[{i}]','region_px':b,'anchor_px':[b[0],140]} for i,b in enumerate(boxes,1)]
 canvas,placements,mapping=render_labels(crop,targets,boxes,font=_evidence_question_label_font(),color=(0,80,160),gap=12)
 for p,b in zip(placements,boxes):
  assert p['placement_mode']=='attached_horizontal_slot'
  assert b[0]-p['label_bbox'][2] == 1
  assert p['connector_line']==[]
  assert p['label_opacity']==1
 assert np.array_equal(np.asarray(canvas)[crop[:,:,0]!=255],crop[crop[:,:,0]!=255])


def test_dense_slots_keep_complete_column_and_restore_original_pixels(tmp_path):
 crop=np.full((300,500,3),254,np.uint8)
 boxes=[[1,0,220,80],[220,0,499,80],[1,80,220,160]]
 for i,(x1,y1,x2,y2) in enumerate(boxes):crop[y1:y2,x1:x2]=(20+i*40)
 targets=[{'question_number':'2','slot_id':f'2[{i}]','region_px':b,'anchor_px':[b[0],(b[1]+b[3])/2]} for i,b in enumerate(boxes,1)]
 canvas,p,m=render_labels(crop,targets,boxes,font=_evidence_question_label_font(),color=(0,80,160))
 assert 'slot_card_panel' not in m
 metadata=source_patch_pnginfo(crop,p,m)
 path=tmp_path/'evidence.png'
 canvas.save(path,pnginfo=metadata)
 with Image.open(path) as saved:
  restored=restore_source_patches(saved,np.array(saved.crop(m['source_rect_px'])),m)
 assert np.array_equal(restored,crop)
 assert m['foreground_preserved'] is False
 assert m['foreground_preserved_outside_labels'] is True
 assert m['foreground_preserved_outside_translucent_labels'] is True
 assert m['opaque_foreground_pixels'] == 0
 dx,dy=m['source_offset_px']
 for label,b in zip(p,boxes):
  assert 'card_copy_rect_px' not in label
  assert label['placement_mode']==('attached_stacked_slot' if label['slot_id']=='2[2]' else 'attached_horizontal_slot')
  assert label['connector_line']==[]
  assert label['label_bbox'][2]+1==b[0]+dx
 assert next(label for label in p if label['slot_id']=='2[2]')['label_opacity']==.65


def _horizontal(boxes, *, number='1', ids=None):
 return [{'question_number':number,'slot_id':sid,'region_px':b,'anchor_px':[b[0],(b[1]+b[3])/2]}
         for sid,b in zip(ids or [f'{number}[{i}]' for i in range(1,len(boxes)+1)],boxes)]


@pytest.mark.parametrize('separation,opacity',[(-2,.65),(0,.65),(8,.65),(80,1)])
@pytest.mark.parametrize('shade',[0,120,254])
def test_only_preceding_box_collision_blends_at_exact_65_percent(separation,opacity,shade):
 crop=np.full((220,700,3),255,np.uint8)
 boxes=[[100,80,300,160],[300+separation,80,600,160]]
 targets=_horizontal(boxes,ids=['1[3]','1[8]'])
 before=copy.deepcopy(targets)
 blank,baseline,_=render_labels(crop,targets,boxes,font=_evidence_question_label_font(),color=(20,84,178))
 l,t,r,b=baseline[1]['label_bbox']
 glyphs=np.asarray(blank)[t:b+1,l:r+1]
 # Probe a fully covered glyph pixel separately from the new blue outline.
 inner=glyphs[1:-1,1:-1]
 gy,gx=np.argwhere(inner[:,:,0]==inner[:,:,0].min())[0]
 x,y=l+gx+1,t+gy+1
 crop[y,x]=shade
 crop[t,l]=shade
 crop[200,680]=31  # overflow well beyond every answer rectangle
 canvas,placements,mapping=render_labels(crop,targets,boxes,font=_evidence_question_label_font(),color=(20,84,178))
 assert targets==before
 assert canvas.size==(700,220)
 assert placements[0]['label_opacity']==1
 assert placements[1]['label_opacity']==opacity
 assert placements[0]['label_lines']==['1.3']
 assert placements[1]['label_lines']==(['1.8'] if separation==80 else ['1','.8'])
 for p,b in zip(placements,boxes):
  l,t,r,bt=p['label_bbox']
  assert r+1==b[0]
  assert abs((t+bt)/2-(b[1]+b[3])/2)<=.5
  assert p['placement_offset_px']==[0,0]
 actual=np.asarray(canvas)
 if opacity==.65:
  alpha=round(255*.65)/255  # RGBA alpha uses the same 8-bit rounding as the probe.
  expected=np.round(shade*(1-alpha)+np.array([20,84,178])*alpha).astype(np.uint8)
  np.testing.assert_allclose(actual[y,x],expected,atol=1)
  np.testing.assert_allclose(actual[t,l],expected,atol=1)
  assert mapping['blended_foreground_pixels']==2
 else:
  assert (actual[y,x]==shade).all()  # original answer pixels always win
  assert (actual[t,l]==shade).all()
  assert mapping['opaque_foreground_pixels']==0
 assert (actual[t+1,l+1]==255).all()  # still no background fill
 assert (actual[200,680]==31).all()


def test_transparent_background_changes_only_glyph_and_border_pixels_over_preceding_box():
 crop=np.full((220,700,3),255,np.uint8)
 boxes=[[100,70,320,170],[325,70,600,170]]
 targets=_horizontal(boxes)
 blank,labels,_=render_labels(crop,targets,boxes,font=_evidence_question_label_font(),color=(20,84,178))
 l,t,r,b=labels[1]['label_bbox']
 glyph_mask=np.any(np.asarray(blank)[t:b+1,l:r+1]!=255,axis=2)
 assert glyph_mask.any() and (~glyph_mask).any()
 assert glyph_mask[0].all() and glyph_mask[-1].all()
 assert glyph_mask[:,0].all() and glyph_mask[:,-1].all()
 assert not glyph_mask[1,1:-1].any()
 crop[t:b+1,l:r+1]=120  # Synthetic ink under the entire label rectangle.
 actual,placements,mapping=render_labels(crop,targets,boxes,font=_evidence_question_label_font(),color=(20,84,178))
 changed=np.any(np.asarray(actual)[t:b+1,l:r+1]!=crop[t:b+1,l:r+1],axis=2)
 # Very faint antialiasing may round back to the same gray pixel. It may
 # never change a background pixel outside the glyph + border footprint.
 assert changed.any() and not np.any(changed & ~glyph_mask)
 np.testing.assert_array_equal(np.asarray(actual)[t:b+1,l:r+1][~glyph_mask],
                               crop[t:b+1,l:r+1][~glyph_mask])
 assert mapping['blended_foreground_pixels']==int(changed.sum())
 assert all(p['label_style']=='blue-hollow-box-v2' for p in placements)
 first=placements[0]['label_bbox']
 assert np.any(np.all(np.asarray(actual)[first[1]:first[3]+1,first[0]:first[2]+1]==[20,84,178],axis=2))


@pytest.mark.parametrize('number',['1','12.3'])
def test_centered_answers_and_overflow_remain_complete_with_narrow_labels(number):
 source=Image.new('RGB',(1000,400),'white')
 font=_evidence_question_label_font()
 boxes=[[100,100,360,240],[365,100,630,240],[635,100,940,240]]
 targets=_horizontal(boxes,number=number,ids=[f'{number}[{i}]' for i in (10,2,7)])
 original=copy.deepcopy(targets)
 draw=ImageDraw.Draw(source)
 for box,answer,shade in zip(boxes,['-8.25','6/7','31'],[35,150,220]):
  draw.text(((box[0]+box[2])/2,(box[1]+box[3])/2),answer,font=font,fill=(shade,)*3,anchor='mm')
 draw.text((370,245),'x = -12.5',font=font,fill=(35,)*3)  # continuation below all boxes
 crop=np.asarray(source)
 canvas,placements,mapping=render_labels(crop,targets,boxes,font=font,color=(20,84,178))
 ink=np.any(crop!=255,axis=2)
 actual=np.asarray(canvas.crop(mapping['source_rect_px']))
 np.testing.assert_array_equal(actual[ink],crop[ink])
 assert mapping['source_size_px']==list(source.size) and targets==original
 assert [p['label_opacity'] for p in placements]==[1,.65,.65]
 assert mapping['foreground_preserved'] and mapping['blended_foreground_pixels']==0
 assert mapping['attached_slot_label_style']=='blue-hollow-box-v2'


def test_vertical_neighbours_remain_normal_and_hierarchy_stays_explicit():
 crop=np.full((400,700,3),255,np.uint8)
 boxes=[[200,60,400,110],[200,210,400,260]]
 canvas,placements,mapping=render_labels(crop,_horizontal(boxes),boxes,font=_evidence_question_label_font(),color=(20,84,178))
 assert [p['label_text'] for p in placements]==['1.1','1.2']
 assert all(p['label_opacity']==1 for p in placements)
 assert all(p['placement_mode']=='attached_horizontal_slot' for p in placements)
 from services.template_slots import slot_label_lines
 assert slot_label_lines('12.3','12.3[10]')==['12','.3','.10']
 with pytest.raises(ValueError):slot_label_lines('12.3','12[10]')


@pytest.mark.parametrize('boxes',[
 [[0,0,150,20],[150,0,300,20]],
 [[0,180,150,200],[150,180,300,200]],
])
def test_edge_padding_keeps_labels_attached_without_clipping(boxes):
 crop=np.full((200,320,3),255,np.uint8)
 canvas,placements,mapping=render_labels(crop,_horizontal(boxes),boxes,font=_evidence_question_label_font(),color=(20,84,178))
 dx,dy=mapping['source_offset_px']
 for p,b in zip(placements,boxes):
  l,t,r,bt=p['label_bbox']
  assert 0<=l<=r<canvas.width and 0<=t<=bt<canvas.height
  assert r+1==b[0]+dx
  assert abs((t+bt)/2-((b[1]+b[3])/2+dy))<=.5


def test_label_collision_remains_teacher_review_not_silent_relocation():
 crop=np.full((200,400,3),255,np.uint8)
 boxes=[[100,60,110,100],[111,60,121,100]]
 with pytest.raises(ValueError,match='第 1 题.*教师复核') as caught:
  render_labels(crop,_horizontal(boxes),boxes,font=_evidence_question_label_font(),color=(20,84,178))
 from services.processing_errors import describe_failure
 public=describe_failure(caught.value,stage='template')
 assert public['code']=='TEMPLATE_SCHEMA_INVALID'
 assert public['retryable'] is False
 assert '第 1 题' in public['message'] and '同一行几乎重叠' in public['message']


@pytest.mark.parametrize('damage',['missing','digest','rectangle','bytes'])
def test_source_pixel_replay_rejects_missing_or_tampered_metadata(tmp_path,damage):
 import base64,hashlib,json,zlib
 from PIL.PngImagePlugin import PngInfo
 crop=np.full((200,600,3),255,np.uint8)
 boxes=[[100,50,290,130],[290,50,500,130]]
 crop[70:100,270:285]=20
 canvas,p,m=render_labels(crop,_horizontal(boxes),boxes,font=_evidence_question_label_font(),color=(20,84,178))
 path=tmp_path/'input.png'
 canvas.save(path,pnginfo=source_patch_pnginfo(crop,p,m))
 with Image.open(path) as saved:
  payload=saved.info['label_source_patches_v1']
 if damage=='digest':payload+=' '
 if damage in {'rectangle','bytes'}:
  parsed=json.loads(payload)
  if damage=='rectangle':parsed[0]['rect'][2]=10000
  else:parsed[0]['rgb']=base64.b64encode(zlib.compress(b'x'*50000)).decode('ascii')
  payload=json.dumps(parsed)
  m['source_patch_sha256']=hashlib.sha256(payload.encode('ascii')).hexdigest()
 metadata=PngInfo()
 if damage!='missing':metadata.add_text('label_source_patches_v1',payload)
 canvas.save(path,pnginfo=metadata)
 with Image.open(path) as saved:
  with pytest.raises(ValueError,match='label source'):
   restore_source_patches(saved,np.array(saved.crop(m['source_rect_px'])),m)
