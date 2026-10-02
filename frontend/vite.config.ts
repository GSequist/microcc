import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Relative base: the built assets are served by FastAPI out of the installed
  // wheel (micro_cc/webui/dist), whose mount path isn't known at build time.
  base: './',
  build: {
    outDir: '../src/micro_cc/webui/dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // `npm run dev` against a running `microcc /path --gui-port 8765`
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
});
