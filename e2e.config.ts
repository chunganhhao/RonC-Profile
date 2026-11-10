import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';

export default {
  tests: 'tests/**/*.e2e.ts',
  workers: 1,
  targets: [{
    engine: web(),
    app: {
      url: 'http://127.0.0.1:18763',
      permissions: { 'clipboard-write': 'grant', 'clipboard-read': 'grant' },
      command: {
        executable: '.venv/bin/python',
        args: ['-m', 'tests.e2e.profiles_fixture', '--port', '{port}'],
        reuseExisting: false,
        log: '.e2e/logs/profiles-fixture.log',
      },
      readyUrl: 'http://127.0.0.1:18763/api/v1/info',
    },
  }],
  trace: 'on',
} satisfies E2EConfig;
