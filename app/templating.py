"""Shared Jinja2Templates instance with project-wide filters registered."""
from fastapi.templating import Jinja2Templates

from app.classify import derive_fallback_preview
from app.duedate import display as due_display
from app.markdown_render import render_markdown

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["markdown"] = render_markdown
templates.env.filters["preview_fallback"] = derive_fallback_preview
templates.env.filters["due_display"] = due_display
