# Contributing to AI-LINE-Friend-Bot

Thank you for your interest in contributing! This project is a LINE chatbot with memory, affinity system, and web summarization features.

## How to Contribute

1. **Fork the repository** and clone your fork locally.
2. **Create a branch** for your changes: `git checkout -b feature/your-feature-name`
3. **Make your changes** and ensure they follow the existing code style.
4. **Test your changes** locally (run the bot with your own LINE tokens if possible).
5. **Commit** with clear messages: `git commit -m "feat: add new feature"`
6. **Push** to your fork and open a Pull Request.

## Development Setup

```bash
pip install -r requirements.txt
cp env.example .env
# Edit .env with your tokens
uvicorn bot:app --reload
```

## Code Style & Guidelines

- Follow PEP 8 where reasonable.
- Keep persona and rules in `persona.txt` / `rules.txt`.
- Do not commit `.env`, `affinity.json`, `memory_db.json`, or `docs_db.json`.
- Use meaningful variable names and add comments for complex logic (memory compression, affinity calculation, debounce).
- For new features, update `README.md` and `config.yaml` comments if applicable.

## Reporting Issues

- Use GitHub Issues.
- Provide steps to reproduce, expected vs actual behavior, and environment info (Python version, etc.).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

Thank you!
