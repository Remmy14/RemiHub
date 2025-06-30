import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({  plugins: [react()],
  base: mode === 'production' ? '/race/' : '/', // 👈 key line
  server: {
    host: '0.0.0.0',
    port: 5173
  }
}))
