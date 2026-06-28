from __future__ import annotations
import questionary

ESC = object()


def _with_escape(question):
    kb = question.application.key_bindings
    add = getattr(kb, "add", None)
    if add is not None:
        @add("escape")
        def _(event):
            event.app.exit(result=ESC)
    else:
        # _MergedKeyBindings (text/confirm/password) : pas de .add → on merge un binding
        try:
            from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
        except ImportError:
            pass
        else:
            extra = KeyBindings()

            @extra.add("escape")
            def _(event):
                event.app.exit(result=ESC)

            question.application.key_bindings = merge_key_bindings([kb, extra])
    return question.ask()


def select(message, choices, **kw):
    return _with_escape(questionary.select(message, choices=choices, **kw))


def text(message, **kw):
    return _with_escape(questionary.text(message, **kw))


def confirm(message, *, default=False, **kw):
    return _with_escape(questionary.confirm(message, default=default, **kw))


def password(message, **kw):
    return _with_escape(questionary.password(message, **kw))
