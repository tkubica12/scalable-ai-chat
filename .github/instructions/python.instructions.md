---
applyTo: '**/*.py'
---
- When writing APIs in Python in this project we focus on FastAPI.
- When accessing Azure services prefer AAD-based authentication and Managed Identities where possible.
- Use `uv` as package manager for Python together with pyproject.toml for dependency management.
- For ease of local development prefer using different default local port for each service.
- Always use docstrings for all public methods and classes. Docstrings are main way to document code, use comments only for complex logic that cannot be expressed in docstrings.
- Never add comments that are describing only changes you have made, such as "Added new method to handle X".  