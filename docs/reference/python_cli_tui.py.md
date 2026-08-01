# File: `tui.py`

## Classes

### `class TalaTUI`
A Textual UI for Tala.

- **`def compose(self)`**
- **`def on_input_submitted(self, event)`**

## Functions

### `def main()`
*No docstring provided.*

## Source Code

```python
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Log, Input
from textual.containers import Vertical

class TalaTUI(App):
    """A Textual UI for Tala."""
    CSS = """
    #chat-log {
        height: 1fr;
        border: solid green;
    }
    #input {
        dock: bottom;
    }
    """
    
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            self.log_widget = Log(id="chat-log")
            yield self.log_widget
            self.input_widget = Input(placeholder="Type a message to Tala...", id="input")
            yield self.input_widget
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value
        if message:
            self.log_widget.write_line(f"You: {message}")
            self.input_widget.value = ""
            # TODO: Send message to Tala server over WebSockets and await response
            self.log_widget.write_line(f"Tala: Echoing back '{message}'")

def main():
    app = TalaTUI()
    app.run()

if __name__ == "__main__":
    main()

```
