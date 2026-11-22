# -*- coding: utf-8 -*-
"""
QiBa Knowledge Fabric - Custom ML Module

Provides offline-only embeddings and reranking capabilities for enterprise environments
that cannot access HuggingFace or other public model hubs.

This module enforces HF_HUB_OFFLINE=1 to prevent any network calls.
"""

__version__ = "1.0.0"

