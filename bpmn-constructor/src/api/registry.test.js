jest.mock('./client', () => ({
    apiClient: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

import { registryApi } from './registry';
import { apiClient } from './client';

describe('registryApi', () => {
    it('moves a diagram using the documented payload', async () => {
        apiClient.post.mockResolvedValue({ data: {} });
        await registryApi.moveDiagram({ diagramId: 'diagram-1', folderId: 'folder-2' });
        expect(apiClient.post).toHaveBeenCalledWith('/api/diagrams/move-to-folder', {
            diagram_id: 'diagram-1', folder_id: 'folder-2',
        });
    });
});
