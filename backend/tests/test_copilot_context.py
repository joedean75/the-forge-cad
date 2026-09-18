"""Initial context and continuation behaviour, with a deterministic provider."""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from main import app
from app.models.schemas import (AssemblyComponent, AssemblyDoc, AssemblyMate, BBox,
                                ComponentTransform, DocumentParameter, Vector3)
from app.services import (action_space, assembly_service, cad_operations as ops,
                          copilot, copilot_context, document_service, llm_client, observation)

client = TestClient(app)
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def doc_id():
    return client.post('/api/documents', json={'resolution': 32}).json()['id']


def provider(monkeypatch, decisions, tools=True):
    calls = []
    queue = iter(decisions)
    monkeypatch.setattr(llm_client, 'is_configured', lambda: True)
    monkeypatch.setattr(llm_client, 'supports_tools', lambda: tools)

    def chat(messages, **options):
        calls.append({'messages': messages, **options})
        decision = next(queue)
        if not tools:
            return {'content': json.dumps(decision)}
        if decision.get('done'):
            return {'content': decision.get('summary', 'done')}
        return {'tool_calls': [{'function': {'name': decision['op'], 'arguments': json.dumps(decision.get('args', {}))}}]}

    monkeypatch.setattr(llm_client, '_post_chat', chat)
    return calls


def snapshot(call):
    content = call['messages'][-1]['content']
    if isinstance(content, list):
        content = content[0]['text']
    return json.loads(content.split('Current document:\n', 1)[1].split('\n\nSteps so far', 1)[0])


def test_initial_part_snapshot_contains_real_constraints_and_driving_parameters():
    doc = document_service.get_document(doc_id())
    doc.source_prompt = 'A lightweight mount'
    doc.spec = {'goal': 'Fit the motor', 'requirements': [{'kind': 'bounding_box', 'axis': 'x', 'value': 100, 'tolerance': 0.2}]}
    doc.parameters = [DocumentParameter(name='width', expr='100', value=100, unit='mm')]
    doc.manufacturing = {'process': 'additive', 'nozzle_mm': 0.4, 'clearance_mm': 0.25}
    doc.general_tolerance = 'm'
    state = observation.summarize(doc)
    assert state['spec'] == doc.spec
    assert state['parameters'][0]['expr'] == '100'
    assert state['manufacturing']['clearance_mm'] == 0.25
    assert state['source_prompt'] == 'A lightweight mount'
    assert state['general_tolerance'] == 'm'


def test_assembly_snapshot_distinguishes_rotation_faces_scale_and_connector_frames(monkeypatch):
    comp = AssemblyComponent(id='motor', part_id='p', name='Motor',
        bbox=BBox(min=Vector3(x=0, y=0, z=0), max=Vector3(x=.02, y=.01, z=.005)),
        transform=ComponentTransform(translate=Vector3(x=.1, y=0, z=0),
                                     rotate_deg=Vector3(x=0, y=0, z=90), scale=2))
    asm = AssemblyDoc(id='a', name='Fixture', created_at='', updated_at='', components=[comp],
        mates=[AssemblyMate(id='m', type='fasten', base='base', moved='motor',
                            base_face='min', moved_face='center', base_connector='b', moved_connector='shaft')])
    monkeypatch.setattr(assembly_service, 'connector_frames', lambda _: {'p': {
        'shaft': (np.array([.01, 0, 0]), np.eye(3)),
    }})
    state = observation.summarize_assembly(asm)
    motor = state['components'][0]
    assert motor['rotation_deg']['z'] == 90
    assert motor['scale'] == 2
    assert motor['world_bbox_mm'] == {'min': {'x': 80, 'y': 0, 'z': 0}, 'max': {'x': 100, 'y': 40, 'z': 10}}
    assert motor['connectors'][0]['world_origin_mm'] == [100, 20, 0]
    assert state['mates'][0]['base_face'] == 'min'
    assert state['mates'][0]['moved_connector'] == 'shaft'
    comp.transform.rotate_deg.z = 0
    asm.mates[0].base_face = 'max'
    assert observation.summarize_assembly(asm) != state


@pytest.mark.parametrize('subject', ['documents', 'assemblies'])
def test_http_selection_and_budget_reach_the_first_model_call(monkeypatch, subject):
    calls = provider(monkeypatch, [{'done': True}])
    if subject == 'documents':
        id_ = doc_id()
        feature = document_service.get_document(id_).feature_tree['root']['id']
        selected = {'feature_id': feature}
    else:
        id_ = client.post('/api/assemblies', json={'name': 'Test'}).json()['id']
        part = doc_id()
        # A single component requires no mate to count as a finished assembly.
        response = client.post(f'/api/assemblies/{id_}/op', json={'op': 'insert_part', 'args': {'part_id': part}})
        assert response.status_code == 200, response.text
        selected = {'component_id': response.json()['result']['component_id']}
    res = client.post(f'/api/{subject}/{id_}/copilot', json={
        'goal': 'Inspect this', 'max_steps': 4, 'context': {'selection': selected},
    })
    assert res.status_code == 200, res.text
    state = snapshot(calls[0])['request_context']
    assert not state['selection']['unresolved']
    assert state['selection']['requested'][next(iter(selected))] == next(iter(selected.values()))
    assert state['budget']['planner_turns_remaining'] == 4
    assert state['budget']['seconds_remaining'] > 0


def test_foreign_selection_never_loads_a_foreign_document(monkeypatch):
    doc = document_service.get_document(doc_id())
    def forbidden(_):
        raise AssertionError('Selection must not fetch any other document')
    monkeypatch.setattr(document_service, 'get_document', forbidden)
    state = copilot_context.resolve_selection('document', doc,
        copilot_context.Selection(feature_id='foreign-id'), observation.summarize(doc))
    assert state['unresolved'] == ['foreign-id']


@pytest.mark.parametrize('tools', [True, False])
def test_discovery_loads_missing_schemas_before_execution(monkeypatch, tools):
    id_ = doc_id()
    initial = copilot_context.initial_tools('document', 'Make a plate', {})
    hidden = next(name for name in ops.OPERATIONS if name not in initial
                  and ops.OPERATIONS[name].subject == 'document' and ops.OPERATIONS[name].kind == 'query'
                  and not ops.OPERATIONS[name].params)
    calls = provider(monkeypatch, [
        {'op': 'discover_operations', 'args': {'names': [hidden]}},
        {'op': hidden}, {'done': True},
    ], tools=tools)
    out = copilot.run(id_, 'Make a plate', max_steps=3)
    assert out['steps'][0]['kind'] == 'discovery'
    assert out['steps'][1]['op'] == hidden and out['steps'][1]['ok']
    assert snapshot(calls[1])['request_context']['budget']['planner_turns_remaining'] == 2
    if tools:
        before = {t['function']['name'] for t in calls[0]['tools']}
        after = {t['function']['name'] for t in calls[1]['tools']}
        assert hidden not in before and hidden in after
        assert len(json.dumps(calls[0]['tools'])) < len(json.dumps(action_space.tools('document'))) * .65
    else:
        assert f'"name": "{hidden}"' not in calls[0]['messages'][0]['content']
        assert f'"name": "{hidden}"' in calls[1]['messages'][0]['content']
        assert 'reply with plain text' not in calls[0]['messages'][0]['content']


def test_discovery_refuses_other_subjects_and_invalid_names():
    for name in ['insert_part', 'invented_operation']:
        with pytest.raises(ValueError):
            copilot_context.discover('document', {'names': [name]})


def test_budget_stopped_work_plan_is_returned_and_carried_into_followup(monkeypatch):
    id_ = doc_id()
    plan = {'objective': 'Build motor mount', 'requirements': ['100 mm base', 'M4 holes'],
            'completed': ['Base exists'], 'remaining': ['Drill mounting holes'], 'decisions': ['Use 0.25 mm clearance']}
    calls = provider(monkeypatch, [{'op': 'update_work_plan', 'args': plan}, {'done': True}])
    path = f'/api/documents/{id_}/copilot'
    res = client.post(path, json={'goal': 'Build motor mount', 'max_steps': 1})
    first = client.get(f"{path}/{res.json()['job_id']}").json()
    assert first['stop_reason'] == 'budget'
    assert first['task_state'] == plan
    res = client.post(path, json={'goal': 'Continue', 'context': {'task_state': first['task_state']}})
    assert res.status_code == 200
    assert snapshot(calls[1])['request_context']['task_state'] == plan


def test_partial_plan_update_keeps_requirements_and_decisions(monkeypatch):
    plan = copilot_context.WorkPlan(objective='Mount motor', requirements=['M4 holes'],
                                    decisions=['Use 0.25 mm clearance'], remaining=['Drill']).model_dump()
    provider(monkeypatch, [{'op': 'update_work_plan', 'args': {'remaining': ['Verify hole locations']}}])
    out = copilot.run(doc_id(), 'Continue', max_steps=1, context={'task_state': plan})
    assert out['task_state']['requirements'] == ['M4 holes']
    assert out['task_state']['decisions'] == ['Use 0.25 mm clearance']
    assert out['task_state']['remaining'] == ['Verify hole locations']


def test_answering_a_status_question_does_not_erase_unfinished_design_work(monkeypatch):
    plan = copilot_context.WorkPlan(objective='Build mount', remaining=['Drill holes']).model_dump()
    provider(monkeypatch, [{'done': True, 'summary': 'The holes still need drilling.'}])
    out = copilot.run(doc_id(), 'What remains?', context={'task_state': plan})
    assert out['task_state']['remaining'] == ['Drill holes']


def test_nested_part_receives_focused_interfaces_requirements_and_selected_images(monkeypatch):
    part = document_service.get_document(doc_id())
    part.spec = {'goal': 'Motor interface', 'requirements': [{'kind': 'hole', 'diameter': 4.5}]}
    part.manufacturing = {'clearance_mm': 0.25}
    asm_id = client.post('/api/assemblies', json={'name': 'Rig'}).json()['id']
    asm = assembly_service.add_component(asm_id, part.id)
    component = asm.components[0].id
    calls = provider(monkeypatch, [
        {'op': 'add_primitive', 'args': {'shape': 'box', 'params': {'size': {'x': .1, 'y': .05, 'z': .01}}}},
        {'done': True, 'summary': 'Built the bracket'},
    ])
    out = copilot.run(asm_id, 'Mount a NEMA 17 motor', subject='assembly', max_steps=1,
        images=[PNG, PNG], history='Use a 100 mm base and 0.25 mm clearance',
        planner=lambda *a: {'op': 'build_part', 'args': {'goal': 'A 100 mm bracket',
            'component_ids': [component], 'requirements': ['Align the M4 holes'], 'image_indices': [1]}})
    assert out['steps'][0]['ok']
    brief = snapshot(calls[0])['parent_assembly_brief']
    assert brief['assembly_goal'] == 'Mount a NEMA 17 motor'
    assert '0.25 mm' in brief['conversation_context']
    assert brief['part_requirements'] == ['Align the M4 holes']
    assert brief['interfaces'][0]['part_design']['manufacturing']['clearance_mm'] == .25
    assert brief['reference_image_indices'] == [1]
    content = calls[0]['messages'][-1]['content']
    # The goal, the image's own label, then the image. Every image is introduced
    # by name so that a review render attached later in the run cannot be
    # mistaken for one of the user's references.
    assert [part['type'] for part in content] == ['text', 'text', 'image_url']
    assert copilot.nested_context() is None


def test_invalid_viewport_reference_is_rejected_before_job_creation():
    response = client.post(f'/api/documents/{doc_id()}/copilot', json={
        'goal': 'Inspect the view', 'context': {'viewport': {'image_index': 0,
            'camera_direction': [0, 0, -1], 'camera_up': [0, 1, 0], 'projection': 'perspective'}},
    })
    assert response.status_code == 422


def test_units_rule_preserves_millimetre_arguments():
    prompt = action_space.system_prompt(with_tools=True, subject='assembly')
    assert 'ALL lengths are in METRES' not in prompt
    assert 'offset_mm is 10' in prompt
    assert 'place_component can rotate' in prompt
