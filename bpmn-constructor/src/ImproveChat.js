// ImproveChat.js — чат улучшения текущей BPMN-схемы.
// Реализация переехала в AiChat.jsx (режим «improve»): два чата были
// идентичны примерно на 95 %. Обёртка сохраняет имя экспорта, чтобы
// Editor.js не менять.
import React from 'react';
import AiChat from './AiChat';

const ImproveChat = ({ onImprove, ...rest }) => (
    <AiChat
        mode="improve"
        onAction={onImprove}
        {...rest}
    />
);

export default ImproveChat;
