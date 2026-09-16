import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: { environment: 'jsdom', include: ['tests/frontend/**/*.test.js'],
    coverage: { include: ['app/static/settings.js'], reporter: ['text', 'json-summary'], thresholds: { statements: 85, branches: 85, functions: 85, lines: 85 } } },
});
