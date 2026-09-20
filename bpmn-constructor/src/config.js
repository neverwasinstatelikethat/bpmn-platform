// Единая точка конфигурации адреса backend.
// Переопределяется переменной окружения сборки REACT_APP_API_URL
// (например, в Docker-сборке); по умолчанию — локальная разработка.
export const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8765';
