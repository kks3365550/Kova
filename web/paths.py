# -*- coding: utf-8 -*-
"""
web/paths.py

공통 경로 상수 정의 모듈
"""
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
