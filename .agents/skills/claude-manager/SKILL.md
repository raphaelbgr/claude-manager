```markdown
# claude-manager Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches the core development patterns and conventions used in the `claude-manager` TypeScript codebase. It covers file naming, import/export styles, commit message conventions, and testing patterns. Use this guide to maintain consistency and efficiency when contributing to or managing the project.

## Coding Conventions

### File Naming
- Use **PascalCase** for all file names.
  - Example: `UserManager.ts`, `ApiClient.ts`

### Import Style
- Use **relative imports** for referencing modules within the project.
  - Example:
    ```typescript
    import { UserManager } from './UserManager';
    ```

### Export Style
- Use **named exports** for all modules.
  - Example:
    ```typescript
    export const UserManager = { /* ... */ };
    ```

### Commit Messages
- Follow the **Conventional Commits** format.
- Use the `docs` prefix for documentation changes.
  - Example: `docs: update README with usage examples`

## Workflows

### Documentation Update
**Trigger:** When updating or improving documentation files.
**Command:** `/update-docs`

1. Make necessary changes to documentation files (e.g., `README.md`, `SKILL.md`).
2. Stage and commit your changes using the `docs` prefix:
   ```
   git add README.md
   git commit -m "docs: clarify setup instructions"
   ```
3. Push your changes and open a pull request if needed.

### Add or Update Code Module
**Trigger:** When adding a new module or updating an existing one.
**Command:** `/add-module`

1. Create or update the file using PascalCase naming (e.g., `NewFeature.ts`).
2. Use relative imports and named exports in your code.
   ```typescript
   // NewFeature.ts
   export function newFeature() { /* ... */ }
   ```
3. Import the module where needed:
   ```typescript
   import { newFeature } from './NewFeature';
   ```
4. Commit your changes following the conventional commit format.
5. Push and open a pull request if applicable.

### Run Tests
**Trigger:** When you want to verify code correctness.
**Command:** `/run-tests`

1. Identify test files matching the `*.test.*` pattern (e.g., `UserManager.test.ts`).
2. Run your project's test runner (framework is unknown; refer to project documentation or package scripts).
   ```
   npm test
   ```
3. Review test results and address any failures.

## Testing Patterns

- Test files are named using the `*.test.*` pattern (e.g., `ApiClient.test.ts`).
- The specific testing framework is not detected; check the project's documentation or dependencies for details.
- Place tests alongside or near the modules they verify.

## Commands

| Command         | Purpose                                 |
|-----------------|-----------------------------------------|
| /update-docs    | Update or improve documentation files   |
| /add-module     | Add or update a TypeScript module       |
| /run-tests      | Run the test suite                      |
```