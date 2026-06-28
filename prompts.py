from __future__ import annotations
import questionary

ESC = object()


def _with_escape(question):
    @question.application.key_bindings.add("escape")
    def _(event):
        event.app.exit(result=ESC)
    return question.ask()


def select(message, choices, **kw):
    return _with_escape(questionary.select(message, choices=choices, **kw))


def text(message, **kw):
    return _with_escape(questionary.text(message, **kw))


def confirm(message, *, default=False, **kw):
    return _with_escape(questionary.confirm(message, default=default, **kw))


def password(message, **kw):
    return _with_escape(questionary.password(message, **kw))
