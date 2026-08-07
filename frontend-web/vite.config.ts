import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import firebaseBrowserConfig from './src/firebase-browser/firebaseBrowserConfig.json' with { type: 'json' }

const requiredFirebaseBrowserConfigKeys = [
  'apiKey',
  'authDomain',
  'projectId',
  'storageBucket',
  'messagingSenderId',
  'appId',
] as const

function validateFirebaseBrowserConfig() {
  const missing = requiredFirebaseBrowserConfigKeys.filter((key) => {
    const value = firebaseBrowserConfig[key]
    return typeof value !== 'string' || value.trim().length === 0
  })

  if (missing.length > 0) {
    throw new Error(
      `Missing Firebase browser configuration keys: ${missing.join(', ')}`,
    )
  }
}

// https://vite.dev/config/
export default defineConfig(() => {
  validateFirebaseBrowserConfig()

  return {
    plugins: [react()],
    base: '/',
    server: {
      host: '0.0.0.0',
      port: 5173,
    },
  }
})
