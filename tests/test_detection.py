"""Tests for detection types."""

import pytest
from browser_handoff.detection import Detection
from browser_handoff.detection.combinators import AllDetection, AnyDetection, NotDetection
from browser_handoff.detection.content import ContentDetection
from browser_handoff.detection.element import ElementDetection
from browser_handoff.detection.url import UrlDetection


class TestDetectionFactory:
    """Tests for the Detection factory class."""

    def test_content_creation(self):
        """Test creating content detection via factory."""
        detection = Detection.content(
            title_contains=["Test"],
            body_contains=["Hello"],
        )
        assert isinstance(detection, ContentDetection)
        assert detection.title_contains == ["Test"]
        assert detection.body_contains == ["Hello"]

    def test_url_creation(self):
        """Test creating URL detection via factory."""
        detection = Detection.url(
            host_equals=["localhost"],
            path_matches=["/callback"],
        )
        assert isinstance(detection, UrlDetection)
        assert detection.host_equals == ["localhost"]
        assert detection.path_matches == ["/callback"]

    def test_element_creation(self):
        """Test creating element detection via factory."""
        detection = Detection.element(
            present=["#login"],
            missing=[".error"],
        )
        assert isinstance(detection, ElementDetection)
        assert detection.present == ["#login"]
        assert detection.missing == [".error"]

    def test_all_combinator(self):
        """Test creating ALL combinator."""
        detection = Detection.all([
            Detection.content(title_contains=["Test"]),
            Detection.url(path_matches=["/test"]),
        ])
        assert isinstance(detection, AllDetection)
        assert len(detection.conditions) == 2

    def test_any_combinator(self):
        """Test creating ANY combinator."""
        detection = Detection.any([
            Detection.content(title_contains=["Test"]),
            Detection.url(path_matches=["/test"]),
        ])
        assert isinstance(detection, AnyDetection)
        assert len(detection.conditions) == 2

    def test_not_combinator(self):
        """Test creating NOT combinator."""
        detection = Detection.not_(
            Detection.element(present=[".error"])
        )
        assert isinstance(detection, NotDetection)
        assert detection.condition is not None

    def test_from_dict_content(self):
        """Test creating detection from dict - content type."""
        data = {
            "type": "content",
            "title_contains": ["Sign In"],
            "body_contains": ["please log in"],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, ContentDetection)
        assert detection.title_contains == ["Sign In"]

    def test_from_dict_url(self):
        """Test creating detection from dict - URL type."""
        data = {
            "type": "url",
            "host_equals": ["localhost"],
            "query_contains": ["code="],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, UrlDetection)
        assert detection.host_equals == ["localhost"]

    def test_from_dict_element(self):
        """Test creating detection from dict - element type."""
        data = {
            "type": "element",
            "present": ["input[type=password]"],
            "visible": [".consent-modal"],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, ElementDetection)
        assert detection.present == ["input[type=password]"]

    def test_from_dict_all_combinator(self):
        """Test creating detection from dict - all combinator."""
        data = {
            "type": "all",
            "conditions": [
                {"type": "content", "title_contains": ["Test"]},
                {"type": "url", "path_matches": ["/test"]},
            ],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, AllDetection)
        assert len(detection.conditions) == 2

    def test_from_dict_any_combinator(self):
        """Test creating detection from dict - any combinator."""
        data = {
            "type": "any",
            "conditions": [
                {"type": "element", "present": ["#success"]},
                {"type": "content", "body_contains": ["Welcome"]},
            ],
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, AnyDetection)
        assert len(detection.conditions) == 2

    def test_from_dict_not_combinator(self):
        """Test creating detection from dict - not combinator."""
        data = {
            "type": "not",
            "condition": {"type": "element", "present": [".error"]},
        }
        detection = Detection.from_dict(data)
        assert isinstance(detection, NotDetection)
        assert isinstance(detection.condition, ElementDetection)

    def test_from_dict_unknown_type(self):
        """Test that unknown type raises ValueError."""
        data = {"type": "unknown"}
        with pytest.raises(ValueError, match="Unknown detection type"):
            Detection.from_dict(data)


class TestContentDetection:
    """Tests for ContentDetection."""

    def test_to_dict(self):
        """Test serialization to dict."""
        detection = ContentDetection(
            title_contains=["Test"],
            body_matches=[r"\d+"],
        )
        data = detection.to_dict()
        assert data["type"] == "content"
        assert data["title_contains"] == ["Test"]
        assert data["body_matches"] == [r"\d+"]

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "content",
            "title_contains": ["Hello"],
            "title_matches": [r"Test \d+"],
        }
        detection = ContentDetection.from_dict(data)
        assert detection.title_contains == ["Hello"]
        assert detection.title_matches == [r"Test \d+"]


class TestUrlDetection:
    """Tests for UrlDetection."""

    def test_to_dict(self):
        """Test serialization to dict."""
        detection = UrlDetection(
            scheme_equals="https",
            host_equals=["example.com"],
            path_contains=["/api/"],
        )
        data = detection.to_dict()
        assert data["type"] == "url"
        assert data["scheme_equals"] == "https"
        assert data["host_equals"] == ["example.com"]

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "url",
            "host_not_equals": ["blocked.com"],
            "query_contains": ["token="],
        }
        detection = UrlDetection.from_dict(data)
        assert detection.host_not_equals == ["blocked.com"]
        assert detection.query_contains == ["token="]


class TestElementDetection:
    """Tests for ElementDetection."""

    def test_to_dict(self):
        """Test serialization to dict."""
        detection = ElementDetection(
            present=["#login"],
            hidden=[".loader"],
        )
        data = detection.to_dict()
        assert data["type"] == "element"
        assert data["present"] == ["#login"]
        assert data["hidden"] == [".loader"]

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "type": "element",
            "missing": [".error"],
            "visible": [".content"],
        }
        detection = ElementDetection.from_dict(data)
        assert detection.missing == [".error"]
        assert detection.visible == [".content"]


class TestCombinators:
    """Tests for combinator detections."""

    def test_all_to_dict(self):
        """Test AllDetection serialization."""
        detection = AllDetection(conditions=[
            ContentDetection(title_contains=["Test"]),
            UrlDetection(path_matches=["/test"]),
        ])
        data = detection.to_dict()
        assert data["type"] == "all"
        assert len(data["conditions"]) == 2
        assert data["conditions"][0]["type"] == "content"
        assert data["conditions"][1]["type"] == "url"

    def test_any_to_dict(self):
        """Test AnyDetection serialization."""
        detection = AnyDetection(conditions=[
            ElementDetection(present=["#a"]),
            ElementDetection(present=["#b"]),
        ])
        data = detection.to_dict()
        assert data["type"] == "any"
        assert len(data["conditions"]) == 2

    def test_not_to_dict(self):
        """Test NotDetection serialization."""
        detection = NotDetection(
            condition=ElementDetection(present=[".error"])
        )
        data = detection.to_dict()
        assert data["type"] == "not"
        assert data["condition"]["type"] == "element"

    def test_nested_combinators(self):
        """Test nested combinator serialization/deserialization."""
        detection = Detection.all([
            Detection.any([
                Detection.content(title_contains=["A"]),
                Detection.content(title_contains=["B"]),
            ]),
            Detection.not_(Detection.element(present=[".error"])),
        ])

        data = detection.to_dict()
        restored = Detection.from_dict(data)

        assert isinstance(restored, AllDetection)
        assert len(restored.conditions) == 2
        assert isinstance(restored.conditions[0], AnyDetection)
        assert isinstance(restored.conditions[1], NotDetection)
