import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueueCapacity } from './QueueCapacity';

const { getCapacity } = vi.hoisted(() => ({ getCapacity: vi.fn() }));
vi.mock('./sceneApi', () => ({ getSceneQueueCapacity: getCapacity }));
const available = { owner: { active_items: 2, reserved_units: 14, limit_units: 256, available_units: 242 },
  global: { active_items: 5, reserved_units: 20, limit_units: 1024, available_units: 1004 },
  max_generated_assets_per_page: 6 };

describe('QueueCapacity', () => {
  beforeEach(() => getCapacity.mockReset().mockResolvedValue(available));

  it('explains weighted reservations without claiming to reserve the displayed availability', async () => {
    render(<QueueCapacity />);
    fireEvent.click(screen.getByText('队列容量'));
    expect(await screen.findByText(/我的任务预留：14 \/ 256 单位/)).toBeInTheDocument();
    expect(screen.getByText(/系统剩余：1004 \/ 1024 单位/)).toBeInTheDocument();
    expect(screen.getByText(/每页生成或 AI 修改先预留 7 单位/)).toHaveTextContent('不保证下一次提交成功');
  });

  it('can refresh a full queue without submitting or automatically retrying paid work', async () => {
    getCapacity.mockResolvedValueOnce({ ...available, owner: { ...available.owner, reserved_units: 256, available_units: 0 } });
    render(<QueueCapacity />);
    fireEvent.click(screen.getByText('队列容量'));
    expect(await screen.findByText(/队列已满/)).toHaveTextContent('不保证停止计费');
    fireEvent.click(screen.getByRole('button', { name: '刷新队列容量' }));
    await waitFor(() => expect(screen.queryByText(/队列已满/)).not.toBeInTheDocument());
    expect(getCapacity).toHaveBeenCalledTimes(2);
  });

  it('discards stale capacity after a failed refresh and enables explicit retry', async () => {
    render(<QueueCapacity />);
    fireEvent.click(screen.getByText('队列容量'));
    await screen.findByText(/我的任务预留/);
    getCapacity.mockRejectedValueOnce(new Error('offline'));
    fireEvent.click(screen.getByRole('button', { name: '刷新队列容量' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('容量查询失败');
    expect(screen.queryByText(/我的任务预留/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新队列容量' })).toBeEnabled();
  });
});
