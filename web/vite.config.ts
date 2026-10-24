import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const version = readFileSync(new URL('../FlowAgentConsoleInstaller.iss', import.meta.url), 'utf8').match(/^#define AppVersion "([^"]+)"/m)?.[1]
if (!version) throw new Error('缺少客户端版本号')

export default defineConfig({
  define: { __FLOW_CLIENT_VERSION__: JSON.stringify(version) },
  plugins: [react()],
  base: '/console/',
  server: {
    host: '127.0.0.1',
    proxy: { '/console/api': 'http://127.0.0.1:8001', '/api/v1/media': 'http://127.0.0.1:8001' },
  },
})

