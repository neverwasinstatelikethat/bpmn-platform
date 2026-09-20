import { apiClient } from './client';

export const registryApi = {
    getDiagrams: () => apiClient.get('/api/diagrams'),
    getTree: () => apiClient.get('/api/folders/tree'),
    getDeleted: () => apiClient.get('/api/deleted-diagrams'),
    getTeams: () => apiClient.get('/api/teams'),
    createTeam: ({ name, color }) => apiClient.post('/api/teams', { name, color }),
    getTeamDiagrams: (teamId) => apiClient.get(`/api/teams/${teamId}/diagrams`),
    getTeamMembers: (teamId) => apiClient.get(`/api/teams/${teamId}/members`),
    inviteMember: (teamId, { email, role }) => apiClient.post(`/api/teams/${teamId}/invite`, { email, role }),
    shareToTeam: ({ diagramId, teamId }) => apiClient.post('/api/diagrams/share-to-team', {
        diagram_id: diagramId,
        team_id: teamId,
    }),
    getRoles: () => apiClient.get('/api/roles'),
    getDiagram: (diagramId) => apiClient.get(`/api/diagrams/${diagramId}`),
    createFolder: ({ name, parentId = null, teamId = null }) => apiClient.post('/api/folders', {
        name,
        parent_id: parentId,
        team_id: teamId,
    }),
    deleteFolder: (folderId) => apiClient.delete(`/api/folders/${folderId}`, { params: { delete_contents: false } }),
    moveDiagram: ({ diagramId, folderId }) => apiClient.post('/api/diagrams/move-to-folder', {
        diagram_id: diagramId,
        folder_id: folderId,
    }),
    deleteDiagram: (diagramId) => apiClient.delete(`/api/diagrams/${diagramId}`),
    restoreDiagram: (diagramId) => apiClient.post('/api/deleted-diagrams/restore', { diagram_id: diagramId }),
    importDiagram: (formData) => apiClient.post('/api/import-bpmn', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
    }),
};
