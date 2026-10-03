import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { StorageCapacity } from './StorageCapacity';

const { getCapacity } = vi.hoisted(() => ({ getCapacity: vi.fn() }));
vi.mock('./sceneApi', () => ({ getSceneStorageCapacity: getCapacity }));
const available = { owner: { used_bytes: 1024, limit_bytes: 2048, available_bytes: 1024, file_count: 3 },
  global: { used_bytes: 1024, limit_bytes: 4096, available_bytes: 3072, file_count: 3 },
  disk_free_bytes: 4096, min_free_bytes: 1024, includes_uncommitted_files: true };

describe('StorageCapacity', () => {
  beforeEach(() => getCapacity.mockReset().mockResolvedValue(available));

  it('shows physical-file usage and explains historical retention and late failures', async () => {
    render(<StorageCapacity />);
    fireEvent.click(screen.getByText('素材存储'));
    expect(await screen.findByText(/我的素材：1 KiB \/ 2 KiB/)).toHaveTextContent('3 个文件');
    expect(screen.getByText(/系统配额剩余：3 KiB/)).toBeInTheDocument();
    expect(screen.getByText(/历史版本、导出和失败遗留文件均占空间/)).toHaveTextContent('重试可能再次计费');
  });

  it('warns when the disk floor is reached, even with remaining owner quota', async () => {
    getCapacity.mockResolvedValueOnce({ ...available, disk_free_bytes: 1 });
    render(<StorageCapacity />);
    fireEvent.click(screen.getByText('素材存储'));
    expect(await screen.findByText(/存储空间不足/)).toHaveTextContent('已保存内容仍可读取');
    fireEvent.click(screen.getByRole('button', { name: '刷新存储占用' }));
    await waitFor(() => expect(screen.queryByText(/存储空间不足/)).not.toBeInTheDocument());
    expect(getCapacity).toHaveBeenCalledTimes(2);
  });

  it('removes stale usage on refresh failure without deleting or retrying any work', async () => {
    render(<StorageCapacity />);
    fireEvent.click(screen.getByText('素材存储'));
    await screen.findByText(/我的素材/);
    getCapacity.mockRejectedValueOnce(new Error('store unavailable'));
    fireEvent.click(screen.getByRole('button', { name: '刷新存储占用' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('联系管理员检查素材卷');
    expect(screen.queryByText(/我的素材/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新存储占用' })).toBeEnabled();
  });
});
