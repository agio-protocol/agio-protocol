# Contributing to AGIO Protocol

Thank you for your interest in contributing to AGIO. This document outlines how to get involved.

---

## Code of Conduct

All contributors are expected to be respectful, constructive, and professional. Harassment, discrimination, and bad-faith behavior will not be tolerated.

---

## How to Contribute

### Reporting Bugs

1. Check [existing issues](https://github.com/agio-protocol/agio/issues) to avoid duplicates.
2. Open a new issue with:
   - A clear, descriptive title.
   - Steps to reproduce the bug.
   - Expected vs. actual behavior.
   - Environment details (OS, SDK version, chain, etc.).

### Suggesting Features

Open an issue with the `feature-request` label. Include:
- The problem you are trying to solve.
- Your proposed solution or approach.
- Any relevant context (use cases, user stories, reference implementations).

### Submitting Code

1. **Fork** the repository and create a branch from `main`.
2. **Name your branch** descriptively: `fix/batch-settler-timeout`, `feat/solana-adapter`, etc.
3. **Write tests** for any new functionality. Ensure existing tests pass.
4. **Follow the code style** of the project. Run linting before submitting.
5. **Keep PRs focused.** One logical change per pull request.
6. **Write a clear PR description** explaining what changed and why.

### Pull Request Process

1. Submit your PR against `main`.
2. A maintainer will review your PR, usually within 3 business days.
3. Address any requested changes.
4. Once approved, a maintainer will merge the PR.

---

## Development Setup

```bash
# Clone the repo
git clone https://github.com/agio-protocol/agio.git
cd agio

# Install dependencies
npm install

# Run tests
npm test

# Run linter
npm run lint
```

Detailed setup instructions are available in the repository README.

---

## Areas Where Help Is Needed

- Chain adapter implementations (Solana, Polygon)
- SDK improvements (TypeScript, Python)
- Documentation and integration guides
- Security review and testing
- Performance benchmarking

---

## Security Vulnerabilities

If you discover a security vulnerability, **do not open a public issue**. Instead, email security@agiotage.finance with details. We will acknowledge receipt within 48 hours and work with you on responsible disclosure.

---

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (see LICENSE file in the repository root).

---

Questions? Reach out in `#dev-chat` on our [Discord](https://discord.gg/agio) or open a discussion on GitHub.
