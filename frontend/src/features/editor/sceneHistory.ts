import type { Command, SceneResponse } from './sceneTypes';

type LockCommand = Extract<Command, { op: 'set_locked' }>;
export type SceneHistoryTarget =
  | { kind: 'revision'; revision_id: string }
  | { kind: 'locks'; commands: LockCommand[] };

/** Undo a lock gesture with an explicit command, never by bypassing restore's lock guard. */
export function sceneHistoryTarget(base: SceneResponse, commands: Command[] = []): SceneHistoryTarget {
  if (!commands.length || !commands.every((command): command is LockCommand => command.op === 'set_locked'))
    return { kind: 'revision', revision_id: base.revision_id };
  const ids = [...new Set(commands.map(command => command.element_id))];
  return { kind: 'locks', commands: ids.map(element_id => {
    const element = base.scene.elements.find(item => item.id === element_id);
    if (!element) throw new Error('锁定历史对象已不存在，请重新读取页面');
    return { op: 'set_locked', element_id, locked: element.locked };
  }) };
}
