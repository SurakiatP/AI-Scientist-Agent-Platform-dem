import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { bootstrapOwner } from './api';
import './styles.css';

const root = ReactDOM.createRoot(document.getElementById('root')!);
const hash = window.location.hash;
const bootstrap = hash.startsWith('#bootstrap=') ? new URLSearchParams(hash.slice(1)).get('bootstrap') : null;
if (bootstrap !== null) history.replaceState(history.state, '', `${location.pathname}${location.search}`);

function mount() {
  root.render(<React.StrictMode><BrowserRouter><App /></BrowserRouter></React.StrictMode>);
}

if (bootstrap === null) {
  mount();
} else {
  void bootstrapOwner(bootstrap).then(mount, () => {
    root.render(<main role="alert"><h1>Startup could not be completed.</h1><p>Close this page and restart the local app to try again.</p></main>);
  });
}
