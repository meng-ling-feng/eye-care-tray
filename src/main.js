import { createApp } from 'vue';
import App from './App.vue';
import './styles.css';

function waitForBridge() {
  if (window.electronAPI) return Promise.resolve();
  return new Promise((resolve) => {
    window.addEventListener('bridge-ready', resolve, { once: true });
  });
}

const app = createApp(App);

waitForBridge().then(() => app.mount('#app'));
