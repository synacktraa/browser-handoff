# Browser Handoff - Human Intervention Required

## Session Information

- **Session ID**: {{ session_id }}
- **Reason**: {{ reason }}
- **Viewport**: {{ viewport_width }}x{{ viewport_height }}

## Stream Access

To view and interact with the browser session, connect to:

- **Stream URL**: `/stream?session={{ session_id }}`
- **WebSocket**: `/ws?session={{ session_id }}`

## Instructions

1. Open the stream URL in your browser to view the current page state
2. The WebSocket connection allows you to send mouse and keyboard input
3. Complete the required action (e.g., solve CAPTCHA, login, authorize)
4. The session will automatically detect when the task is complete

## Input Events

Send JSON messages over WebSocket to control the browser:

### Mouse Events
```json
{"type": "mouse", "action": "mousedown", "x": 100, "y": 200, "button": 0}
{"type": "mouse", "action": "mouseup", "x": 100, "y": 200, "button": 0}
{"type": "mouse", "action": "mousemove", "x": 100, "y": 200}
{"type": "mouse", "action": "wheel", "x": 100, "y": 200, "deltaX": 0, "deltaY": 100}
```

### Keyboard Events
```json
{"type": "keyboard", "action": "keydown", "key": "a", "code": "KeyA"}
{"type": "keyboard", "action": "keyup", "key": "a", "code": "KeyA"}
```

### Navigation
```json
{"type": "navigate", "action": "reload"}
```

## Server Messages

The server will send messages when events occur:

### Task Completed
```json
{"type": "task_completed", "reason": "URL matched callback pattern"}
```

---

*This session is managed by browser-handoff*
