import tseslint from 'typescript-eslint';

export default tseslint.config({
  files: ['static/ts/**/*.ts'],
  extends: [tseslint.configs.recommended],
  languageOptions: { parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname } },
  rules: {
    '@typescript-eslint/no-explicit-any': 'error',
    '@typescript-eslint/no-floating-promises': 'error',
    '@typescript-eslint/no-unused-vars': 'off', // tsc noUnusedLocals/Parameters covers this
  },
},
  // theme-boot is a classic script; its `var`s are deliberate
  { files: ['static/ts/theme-boot.ts'], rules: { 'no-var': 'off' } },
);
