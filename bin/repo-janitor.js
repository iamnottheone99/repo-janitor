#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(__dirname, '..');

const py = process.platform === 'win32' ? 'python' : 'python3';
const args = ['-m', 'repo_janitor.cli', ...process.argv.slice(2)];

const envPathSep = process.platform === 'win32' ? ';' : ':';
const currentPyPath = process.env.PYTHONPATH ? `${process.env.PYTHONPATH}${envPathSep}` : '';
const newPyPath = `${join(repoRoot, 'src')}${envPathSep}${currentPyPath}`;

const proc = spawn(py, args, {
  cwd: process.cwd(),
  stdio: 'inherit',
  env: {
    ...process.env,
    PYTHONPATH: newPyPath,
  },
});

proc.on('exit', (code) => {
  process.exit(code ?? 0);
});
