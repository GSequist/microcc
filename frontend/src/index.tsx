import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';
import { installGate } from './lib/gate';

installGate();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
