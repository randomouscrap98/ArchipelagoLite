"""Bits shared by the pages aplite serves with upstream's WebHostLib templates (Web Options, Self-host). Flask is only
imported when a page is served."""
from aplite import OFFICIAL_SITE


def page(title: str, body: str) -> str:
    """A Jinja template: upstream's page wrapper and grass header around `body` (HTML, Jinja allowed in both)."""
    return """
{% extends 'pageWrapper.html' %}
{% block head %}
    <title>""" + title + """</title>
    <link rel="stylesheet" type="text/css" href="{{ url_for('static', filename='styles/markdown.css') }}" />
{% endblock %}
{% block body %}
    {% include 'header/grassHeader.html' %}
    <div class="markdown" style="max-width: 60rem; margin: 1rem auto; padding: 1rem 2rem;">""" + body + """    </div>
{% endblock %}
"""


def to_official_site():
    """Redirect the current request to the same page on the official site (general pages the header links to)."""
    from flask import redirect, request
    return redirect(f"{OFFICIAL_SITE}{request.full_path.rstrip('?')}")
