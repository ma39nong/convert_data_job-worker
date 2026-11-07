"""English annotation vocabulary; Chinese remains in the review workspace only."""
import copy

CATALOG = {'profile': 'tomato-coarse-ab-en-v3',
 'goal': 'Harvest all tomatoes into the left basket and return stem to right plate',
 'coarse': {'pickup': 'Right hand grasps and lifts the truss to the center',
            'harvest': 'Right hand holds truss while left hand picks tomatoes into left basket',
            'return': 'Right hand returns the truss to the right plate',
            'adjust': 'Right fingers reposition the truss on the right plate'},
 'fine': {'grasp_truss': 'Grasp and secure the entire tomato truss with the right hand',
          'lift_truss': 'Lift the tomato truss with the right hand and move it to the center',
          'grasp_fruit': 'Approach and firmly grasp one cherry tomato with the left hand while '
                         'holding the tomato truss steady with the right hand',
          'pick_and_place': 'Detach the grasped cherry tomato with the left hand and place it in '
                            'the left basket while holding the tomato truss steady with the right '
                            'hand',
          'return_truss': 'Place the tomato truss back on the right white plate with the right '
                          'hand and release it',
          'rotate_stem': 'Adjust the tomato truss with the right hand',
          'return_stem': 'After all tomatoes have been picked place the remaining stem back on the '
                         'right white plate with the right hand'},
 'return_stem': 'Right hand returns the bare stem to the right plate',
 'numbers': {'unknown': 0, 'pickup': 1, 'harvest': 2, 'return': 3, 'adjust': 4}}
DEFAULT_TASK_PROMPT=CATALOG['goal']
PROMPT_PROFILE=CATALOG['profile']
STAGE_PROMPTS={k:CATALOG[k] for k in ('coarse','fine')}
SEQUENCE=('pickup','harvest','return','adjust','pickup','harvest','return')

def stage_prompt(stage,rows,index):
    label=rows[index]['label']
    if stage=='coarse' and index==6 and tuple(r['label'] for r in rows)==SEQUENCE:
        return CATALOG['return_stem']
    return STAGE_PROMPTS[stage].get(label,'Unlabeled')

def english_document(document):
    """Publish only machine metadata and English labels, retaining reviewed frame ranges.

    Freeform review notes and model explanations stay in versioned workspace files.
    Source identifiers/paths are provenance and must never be translated.
    """
    out={k:copy.deepcopy(v) for k,v in document.items() if k in
         ('schema_version','stage','revision','source','saved_at','projection','inherited')}
    if 'projection' in out:
        out['projection']={k:v for k,v in out['projection'].items() if k not in ('review_events','normal_review_events')}
    out['language']='en'
    out['prompt_profile']=PROMPT_PROFILE
    out['task_prompt']=DEFAULT_TASK_PROMPT
    out['segments']=[]
    for i,row in enumerate(document['segments']):
        item={k:copy.deepcopy(v) for k,v in row.items() if k in
              ('start_frame','end_frame','start_seconds','end_seconds','label','reviewed')}
        item['text']=stage_prompt(document['stage'],document['segments'],i)
        item['segment_index']=i+1
        out['segments'].append(item)
    return out
