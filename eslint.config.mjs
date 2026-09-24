// Lint gate for the JS side (P2 review item 16). Two jobs:
//
//   1. A file-size RATCHET. renderer.js and main.js grew unbounded because
//      nothing pushed back. The per-file caps below are pinned at the size the
//      file had when the ratchet landed — they may go DOWN as code moves out
//      into modules, never up. If this rule fails your change, extract code
//      into a new module; do not raise the number.
//   2. A handful of zero-tolerance safety rules that are clean today and must
//      stay clean.
//
// Deliberately no style rules yet: a 16k-line legacy file re-styled in one
// commit would bury real changes. Broaden rule coverage as files shrink.

const safetyRules = {
  'no-debugger': 'error',
  'no-eval': 'error',
  'no-implied-eval': 'error',
  'no-new-func': 'error',
};

export default [
  {
    ignores: [
      '.venv/**',
      '.tala_snapshots/**',
      'scratch/**',
      'dist/**',
      'nodes/**',
      'data/**',
      'vault/**',
      'blockchain/public/**',
    ],
  },
  {
    files: ['electron/**/*.js', 'scripts/**/*.js', 'tests/**/*.js', 'blockchain/src/**/*.js'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'commonjs',
    },
    rules: {
      ...safetyRules,
      // Default budget for anything new: small enough that the next god-file
      // cannot happen by accident.
      'max-lines': ['error', { max: 800, skipBlankLines: false, skipComments: false }],
    },
  },
  {
    files: ['renderer/**/*.js'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'script',
    },
    rules: {
      ...safetyRules,
      'max-lines': ['error', { max: 800, skipBlankLines: false, skipComments: false }],
    },
  },
  {
    // The two god files, pinned at their current size. Down only.
    // 16515 -> 16455: the 2026-09-23 UI-audit campaign overage paid back by
    // moving LoggingManager.updateSystemHealth out to health_ops.js.
    files: ['renderer/renderer.js'],
    rules: {
      'max-lines': ['error', { max: 16455, skipBlankLines: false, skipComments: false }],
    },
  },
  {
    files: ['electron/main.js'],
    rules: {
      'max-lines': ['error', { max: 1129, skipBlankLines: false, skipComments: false }],
    },
  },
  {
    files: ['blockchain/src/app.js'],
    rules: {
      'max-lines': ['error', { max: 1822, skipBlankLines: false, skipComments: false }],
    },
  },
];
