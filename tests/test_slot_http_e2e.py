"""Actual HTTP save/confirm/reopen with synthetic eight-slot template only."""
import copy
import json
from unittest.mock import AsyncMock
from PIL import Image, ImageDraw
from services.template_session import TemplateSession
from services.template_evidence import build_template_artifact
from services.scoring_engine import build_grading_units
from tests.test_template_session import _make_app_and_session, _client
from tests.test_slot_cache_contract import contract, response


def seed(tmp_path):
    q = contract()
    boxes = [[80 + (i % 2)*450, 150 + (i//2)*170, 450 + (i % 2)*450, 290+(i//2)*170] for i in range(8)]
    anchors = [{'question_number':'2','question_number_bbox':[50,50,90,85], 'confidence':1, 'human_confirmed':True,'confidence_source':'teacher'}]
    regions = [{'question_number':'2','regions':boxes,'slot_ids':q['slot_ids'],'region_kinds':q['region_kinds'], 'confidence':1,'human_confirmed':True,'confidence_source':'teacher'}]
    img = Image.new('RGB',(1000,1000),'white'); draw=ImageDraw.Draw(img)
    draw.text((50,50),'2. Calculate (synthetic test only)',fill='black')
    for i,b in enumerate(boxes,1): draw.text((b[0],b[1]),f'({i}) {100+i}',fill='black')
    source=tmp_path/'source.png'; img.save(source)
    sess=TemplateSession('slot-http-test',str(tmp_path),teacher_id=1)
    artifact=build_template_artifact(str(source),sess.template_dir,0,[q],question_anchors=anchors,template_answer_regions=regions,template_handwriting_regions=regions,template_mode='marked')
    sess.add_page(0,[q],artifact=artifact); sess.status='ocr_done'; sess.category='daily'; sess.template_mode='marked'; sess.save_meta()
    return sess, boxes, anchors


def test_http_eight_slot_swap_save_confirm_reopen_and_score(monkeypatch,tmp_path):
    import routers.grade as grade
    from routers import grade_pipeline
    from routers import grade_template
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade_pipeline,'UPLOAD_DIR',str(tmp_path))
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade,'UPLOAD_DIR',str(tmp_path))
    monkeypatch.setattr(grade_template,'_classify_confirmed_template_knowledge',AsyncMock())
    sess,boxes,anchors=seed(tmp_path); client=_client(factory,monkeypatch,raise_server_exceptions=True)
    q=copy.deepcopy(sess.pages[0]['questions'][0]); changed=copy.deepcopy(boxes)
    changed[1],changed[2]=changed[2],changed[1]
    payload={'page_index':0,'columns':[{'column_index':1,'x1_1000':0,'x2_1000':1000}],
        'question_anchors':anchors,
        'template_answer_regions':[{'question_number':'2','regions':changed,'slot_ids':q['slot_ids'],'region_kinds':q['region_kinds']}],
        'template_handwriting_regions':[{'question_number':'2','regions':changed}],
        'question_schema':[{**q,'question_number':'2','drawing_regions':[],'label_anchor':None,'answer_kind':'non_graphic'}]}
    url=f'/api/batch-grade/template/{sess.template_id}/layout-overrides'
    saved=client.put(url,json=payload); assert saved.status_code==200,saved.text
    reopened=TemplateSession.load_meta(sess.template_id,str(tmp_path)); artifact=reopened.page_artifacts['page_1']
    assert artifact['template_answer_regions'][0]['regions']==changed
    assert artifact['template_answer_regions'][0]['slot_ids']==q['slot_ids']
    assert reopened.pages[0]['questions'][0]['answer_slots']==q['answer_slots']
    assert artifact['question_schema'][0]['slot_ids']==q['slot_ids']
    confirmed=client.post('/api/batch-grade/template/confirm',json={'template_id':sess.template_id,'category':'daily','pages':reopened.pages})
    assert confirmed.status_code==200,confirmed.text
    frozen=TemplateSession.load_meta(sess.template_id,str(tmp_path)); assert frozen.status=='confirmed'
    units=build_grading_units([response()],frozen.build_answer_map(),'daily')
    assert len(units)==8
    assert [u['slot_id'] for u in units]==q['slot_ids']
    assert [u['student_answer'] for u in units]==['101','','103','104','105','106','107','108']
    # Duplicate-ID writes cannot replace the confirmed snapshot.
    before=open(frozen.meta_path,'rb').read(); bad=copy.deepcopy(payload)
    bad['template_answer_regions'][0]['slot_ids'][2]='2[2]'
    rejected=client.put(url,json=bad); assert rejected.status_code==422,rejected.text
    assert open(frozen.meta_path,'rb').read()==before


def test_http_explicit_slot_removal_persists_sparse_ids(monkeypatch,tmp_path):
    import routers.grade as grade
    from routers import grade_pipeline
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade_pipeline,'UPLOAD_DIR',str(tmp_path))
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade,'UPLOAD_DIR',str(tmp_path))
    sess,boxes,anchors=seed(tmp_path); client=_client(factory,monkeypatch,raise_server_exceptions=True)
    q=copy.deepcopy(sess.pages[0]['questions'][0])
    for key in ['slot_ids','region_kinds','answer_slots']: q[key].pop(1)
    q['blank_count']=7; q['subquestion_count']=7
    boxes.pop(1)
    payload={'page_index':0,'columns':[{'column_index':1,'x1_1000':0,'x2_1000':1000}], 'question_anchors':anchors,
        'template_answer_regions':[{'question_number':'2','regions':boxes,'slot_ids':q['slot_ids'],'region_kinds':q['region_kinds']}],
        'template_handwriting_regions':[{'question_number':'2','regions':boxes}],
        'question_schema':[{**q,'question_number':'2'}]}
    res=client.put(f'/api/batch-grade/template/{sess.template_id}/layout-overrides',json=payload)
    assert res.status_code==200,res.text
    reopened=TemplateSession.load_meta(sess.template_id,str(tmp_path))
    assert reopened.pages[0]['questions'][0]['slot_ids']==['2[1]','2[3]','2[4]','2[5]','2[6]','2[7]','2[8]']
    assert reopened.pages[0]['questions'][0]['answer_slots']==['101','103','104','105','106','107','108']


def test_http_deleting_last_drawing_slot_drops_its_figure_and_confirms(monkeypatch,tmp_path):
    """A model drawing slot that sat on the stem is deleted after a figure was saved.

    The question becomes non-graphic; the stale figure must not block either the
    layout save or the confirm (diagnostic 2b5e7b509d74481aa71a931d450af93d).
    """
    import routers.grade as grade
    from routers import grade_pipeline
    from routers import grade_template
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade_pipeline,'UPLOAD_DIR',str(tmp_path))
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade,'UPLOAD_DIR',str(tmp_path))
    monkeypatch.setattr(grade_template,'_classify_confirmed_template_knowledge',AsyncMock())
    stem,text=[80,60,900,140],[80,300,450,420]
    reference={'version':'drawing-reference-v1','coordinate_space':'normalized_1000_v1','comparison_mode':'manual','variants':[]}
    q={'question_no':'3','question_text':'synthetic stem','question_type':'solve','standard_answer':'x=2',
       'answer_slots':['x=2',''],'blank_count':2,'slot_ids':['3[1]','3[2]'],'region_kinds':['answer','drawing'],
       'answer_kind':'mixed','drawing_regions':[stem],'label_anchor':[40,100],'drawing_reference':reference}
    anchors=[{'question_number':'3','question_number_bbox':[40,80,70,120],'confidence':1,'human_confirmed':True,'confidence_source':'teacher'}]
    regions=[{'question_number':'3','regions':[text,stem],'slot_ids':q['slot_ids'],'region_kinds':q['region_kinds'],
              'confidence':1,'human_confirmed':True,'confidence_source':'teacher'}]
    img=Image.new('RGB',(1000,1000),'white'); ImageDraw.Draw(img).text((80,80),'3. synthetic stem',fill='black')
    source=tmp_path/'source.png'; img.save(source)
    sess=TemplateSession('slot-stem-drawing',str(tmp_path),teacher_id=1)
    artifact=build_template_artifact(str(source),sess.template_dir,0,[q],question_anchors=anchors,template_answer_regions=regions,template_handwriting_regions=regions,template_mode='marked')
    sess.add_page(0,[q],artifact=artifact); sess.status='ocr_done'; sess.category='daily'; sess.template_mode='marked'; sess.save_meta()
    assert TemplateSession.load_meta(sess.template_id,str(tmp_path)).pages[0]['questions'][0]['drawing_reference']
    client=_client(factory,monkeypatch,raise_server_exceptions=True)
    kept={'slot_ids':['3[1]'],'region_kinds':['answer']}
    layout={'page_index':0,'columns':[{'column_index':1,'x1_1000':0,'x2_1000':1000}],'question_anchors':anchors,
        'template_answer_regions':[{'question_number':'3','regions':[text],**kept}],
        'template_handwriting_regions':[{'question_number':'3','regions':[text]}],
        'question_schema':[{'question_number':'3','question_type':'solve',**kept,'answer_kind':'non_graphic',
            'drawing_regions':[],'label_anchor':[40,100],'blank_count':1,'answer_slots':['x=2']}]}
    saved=client.put(f'/api/batch-grade/template/{sess.template_id}/layout-overrides',json=layout)
    assert saved.status_code==200,saved.text
    row=TemplateSession.load_meta(sess.template_id,str(tmp_path)).pages[0]['questions'][0]
    assert row['region_kinds']==['answer'] and 'drawing_reference' not in row
    confirmed=client.post('/api/batch-grade/template/confirm',json={'template_id':sess.template_id,'category':'daily',
        'pages':[{'page_index':0,'questions':[{'question_no':'3','question_text':'synthetic stem','question_type':'solve',
            'standard_answer':'x=2','blank_count':1,'answer_slots':['x=2']}]}]})
    assert confirmed.status_code==200,confirmed.text
    frozen=TemplateSession.load_meta(sess.template_id,str(tmp_path)).pages[0]['questions'][0]
    assert frozen['answer_kind']=='non_graphic' and 'drawing_reference' not in frozen


def test_http_regions_arriving_in_another_order_rebind_by_slot_id(monkeypatch,tmp_path):
    """A box drawn later for a kept position arrives last; the slot ID, not the
    list position, is its identity (fuzz finding: 'saved slot IDs disagree')."""
    import routers.grade as grade
    from routers import grade_pipeline
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade_pipeline,'UPLOAD_DIR',str(tmp_path))
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade,'UPLOAD_DIR',str(tmp_path))
    sess,boxes,anchors=seed(tmp_path); client=_client(factory,monkeypatch,raise_server_exceptions=True)
    q=copy.deepcopy(sess.pages[0]['questions'][0])
    order=[7,0,1,2,3,4,5,6]
    payload={'page_index':0,'columns':[{'column_index':1,'x1_1000':0,'x2_1000':1000}],'question_anchors':anchors,
        'template_answer_regions':[{'question_number':'2','regions':[boxes[i] for i in order],'slot_ids':[q['slot_ids'][i] for i in order]}],
        'template_handwriting_regions':[{'question_number':'2','regions':boxes}],
        'question_schema':[{**q,'question_number':'2','drawing_regions':[],'label_anchor':None,'answer_kind':'non_graphic'}]}
    saved=client.put(f'/api/batch-grade/template/{sess.template_id}/layout-overrides',json=payload)
    assert saved.status_code==200,saved.text
    region=TemplateSession.load_meta(sess.template_id,str(tmp_path)).page_artifacts['page_1']['template_answer_regions'][0]
    assert region['slot_ids']==q['slot_ids'] and region['regions']==boxes
    bad=copy.deepcopy(payload); bad['template_answer_regions'][0]['slot_ids'][0]='2[9]'
    rejected=client.put(f'/api/batch-grade/template/{sess.template_id}/layout-overrides',json=bad)
    assert rejected.status_code==422 and '第 2 题作答框的槽编号与题目不一致' in rejected.json()['detail']


def test_http_teacher_deleting_every_position_clears_slots_and_confirms(monkeypatch,tmp_path):
    """Every box of a mixed question deleted: its slot numbers and drawing kind go
    with the boxes (sent as slot_ids null), nothing stale survives to confirm."""
    import routers.grade as grade
    from routers import grade_pipeline
    from routers import grade_template
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade_pipeline,'UPLOAD_DIR',str(tmp_path))
    _,factory=_make_app_and_session(); monkeypatch.setattr(grade,'UPLOAD_DIR',str(tmp_path))
    monkeypatch.setattr(grade_template,'_classify_confirmed_template_knowledge',AsyncMock())
    stem,text=[80,60,900,140],[80,300,450,420]
    q={'question_no':'3','question_text':'synthetic stem','question_type':'solve','standard_answer':'x=2',
       'answer_slots':['x=2',''],'blank_count':2,'slot_ids':['3[1]','3[2]'],'region_kinds':['answer','drawing'],
       'answer_kind':'mixed','drawing_regions':[stem],'label_anchor':[40,100]}
    anchors=[{'question_number':'3','question_number_bbox':[40,80,70,120],'confidence':1,'human_confirmed':True,'confidence_source':'teacher'}]
    regions=[{'question_number':'3','regions':[text,stem],'slot_ids':q['slot_ids'],'region_kinds':q['region_kinds'],
              'confidence':1,'human_confirmed':True,'confidence_source':'teacher'}]
    img=Image.new('RGB',(1000,1000),'white'); ImageDraw.Draw(img).text((80,80),'3. synthetic stem',fill='black')
    source=tmp_path/'source.png'; img.save(source)
    sess=TemplateSession('slot-cleared',str(tmp_path),teacher_id=1)
    artifact=build_template_artifact(str(source),sess.template_dir,0,[q],question_anchors=anchors,template_answer_regions=regions,template_handwriting_regions=regions,template_mode='marked')
    sess.add_page(0,[q],artifact=artifact); sess.status='ocr_done'; sess.category='daily'; sess.template_mode='marked'; sess.save_meta()
    client=_client(factory,monkeypatch,raise_server_exceptions=True)
    layout={'page_index':0,'columns':[{'column_index':1,'x1_1000':0,'x2_1000':1000}],'question_anchors':anchors,
        'template_answer_regions':[{'question_number':'3','regions':[]}],
        'template_handwriting_regions':[{'question_number':'3','regions':[]}],
        'question_schema':[{'question_number':'3','question_type':'solve','slot_ids':None,'region_kinds':[],
            'answer_kind':'non_graphic','drawing_regions':[],'label_anchor':None,'blank_count':1,'answer_slots':[]}]}
    saved=client.put(f'/api/batch-grade/template/{sess.template_id}/layout-overrides',json=layout)
    assert saved.status_code==200,saved.text
    stored=TemplateSession.load_meta(sess.template_id,str(tmp_path))
    row=stored.pages[0]['questions'][0]
    assert 'slot_ids' not in row and 'region_kinds' not in row
    assert row['answer_kind']=='non_graphic' and not row.get('drawing_regions')
    assert 'slot_ids' not in stored.page_artifacts['page_1']['question_schema'][0]
    confirmed=client.post('/api/batch-grade/template/confirm',json={'template_id':sess.template_id,'category':'daily',
        'pages':[{'page_index':0,'questions':[{'question_no':'3','question_text':'synthetic stem','question_type':'solve',
            'standard_answer':'x=2','blank_count':1,'answer_slots':[]}]}]})
    assert confirmed.status_code==200,confirmed.text
    frozen=TemplateSession.load_meta(sess.template_id,str(tmp_path)).pages[0]['questions'][0]
    assert 'slot_ids' not in frozen and frozen.get('answer_kind','non_graphic')=='non_graphic' and not frozen.get('drawing_regions')
