// GenerateChat.js — чат генерации BPMN-схемы.
// Реализация переехала в AiChat.jsx (режим «generate»): два чата были
// идентичны примерно на 95 %. Обёртка сохраняет имя экспорта, чтобы
// Editor.js не менять.
import React from 'react';
import AiChat from './AiChat';

const GenerateChat = ({ onGenerate, ...rest }) => (
    <AiChat mode="generate" onAction={onGenerate} {...rest} />
);

export default GenerateChat;
