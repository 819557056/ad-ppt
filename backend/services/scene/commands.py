"""Apply only named editor operations; locked objects require explicit unlock."""
from copy import deepcopy
from uuid import UUID


class CommandError(ValueError):
    pass


OPS = {'add_element', 'delete_element', 'set_text', 'set_frame', 'set_text_style',
       'replace_image', 'set_crop', 'set_opacity', 'reorder_elements', 'set_background', 'set_locked'}


def apply_commands(base, commands):
    if not isinstance(commands, list) or not 1 <= len(commands) <= 200:
        raise CommandError('commands must have 1..200 items')
    scene = deepcopy(base)
    for command in commands:
        if not isinstance(command, dict) or command.get('op') not in OPS:
            raise CommandError('unsupported command')
        op = command['op']
        allowed = {
            'add_element': {'op', 'element'}, 'delete_element': {'op', 'element_id'},
            'set_text': {'op', 'element_id', 'text'}, 'set_frame': {'op', 'element_id', 'frame'},
            'set_text_style': {'op', 'element_id', 'style'},
            'replace_image': {'op', 'element_id', 'asset_id'},
            'set_crop': {'op', 'element_id', 'crop', 'fit'},
            'set_opacity': {'op', 'element_id', 'opacity'},
            'reorder_elements': {'op', 'element_ids'},
            'set_background': {'op', 'background'},
            'set_locked': {'op', 'element_id', 'locked'},
        }[op]
        if set(command) - allowed:
            raise CommandError('unknown command field')
        required = allowed - ({'fit'} if op == 'set_crop' else set())
        if required - set(command):
            raise CommandError('missing command field')
        if op == 'add_element':
            element = command['element']
            if not isinstance(element, dict) or element.get('locked') is True:
                raise CommandError('new element cannot be locked implicitly')
            try:
                UUID(element['id'])
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise CommandError('element id must be UUID') from exc
            if any(e['id'] == element['id'] for e in scene['elements']):
                raise CommandError('duplicate element id')
            scene['elements'].append(deepcopy(element))
            continue
        if op == 'set_background':
            scene['background'] = deepcopy(command['background'])
            continue
        if op == 'reorder_elements':
            ids = command['element_ids']
            current = scene['elements']
            if (not isinstance(ids, list) or len(ids) != len(current) or
                    any(not isinstance(element_id, str) for element_id in ids) or
                    set(ids) != {e['id'] for e in current}):
                raise CommandError('reorder must include every element exactly once')
            old_locked_positions = {e['id']: index for index, e in enumerate(current) if e['locked']}
            if any(ids.index(eid) != pos for eid, pos in old_locked_positions.items()):
                raise CommandError('locked element cannot change layer')
            by_id = {e['id']: e for e in current}
            scene['elements'] = [by_id[eid] for eid in ids]
            continue
        element_id = command.get('element_id')
        element = next((e for e in scene['elements'] if e['id'] == element_id), None)
        if element is None:
            raise CommandError('element not found')
        if op == 'set_locked':
            if not isinstance(command.get('locked'), bool):
                raise CommandError('locked must be boolean')
            element['locked'] = command['locked']
            continue
        if element['locked']:
            raise CommandError('element is locked')
        if op == 'delete_element':
            scene['elements'].remove(element)
        elif op in ('set_text', 'set_text_style'):
            if element['kind'] != 'text':
                raise CommandError('text command requires text element')
            element['text' if op == 'set_text' else 'style'] = deepcopy(command['text' if op == 'set_text' else 'style'])
        elif op in ('replace_image', 'set_crop', 'set_opacity'):
            if element['kind'] != 'image':
                raise CommandError('image command requires image element')
            if op == 'replace_image':
                element['asset_id'] = command['asset_id']
            elif op == 'set_crop':
                element['crop'] = deepcopy(command['crop'])
                if 'fit' in command:
                    element['fit'] = command['fit']
            else:
                element['opacity'] = command['opacity']
        elif op == 'set_frame':
            element['frame'] = deepcopy(command['frame'])
    return scene


def assert_locked_preserved(old, new):
    old_elements = old['elements']
    new_elements = new['elements']
    for index, element in enumerate(old_elements):
        if not element['locked']:
            continue
        if index >= len(new_elements) or new_elements[index] != element:
            raise CommandError('restore/candidate changes locked element')
    previous = {e['id']: e for e in old_elements}
    for index, element in enumerate(new_elements):
        if previous.get(element['id']) == element:
            continue
        a = element['frame']
        for locked in new_elements[:index]:
            if not locked['locked'] or locked['kind'] != 'text':
                continue
            b = locked['frame']
            overlap = max(0, min(a['x'] + a['w'], b['x'] + b['w']) - max(a['x'], b['x'])) * max(
                0, min(a['y'] + a['h'], b['y'] + b['h']) - max(a['y'], b['y']))
            if overlap > b['w'] * b['h'] * .05:
                raise CommandError('new or changed object obscures locked text')
