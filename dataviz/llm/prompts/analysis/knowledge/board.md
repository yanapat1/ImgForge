---
title: The builder board's notes
summary: zones, text labels, sticky notes and shapes on the Visual builder board; lock, colours, text position, Auto zones, read_board / edit_board
high: true
---
- The Visual builder board holds **notes** next to the table cards, so a schema can be explained in zones. One kind of element covers all of them: a **zone** is a shape behind the tables (layer back) with a light fill and a title; **text** is a shape with no fill or border; a **sticky note** is a filled shape in front; other shapes are ellipse, diamond, hexagon, callout (speech bubble) and pill. A background box with no text is just a coloured zone. Notes never change the SQL and are saved with the query tab (undo covers them).
- **Add**: **+ Note** bottom-right on the board (Zone, Text, Title, Sticky note, shapes, Auto zones), or right-click an empty spot of the board to add one there.
- **Move and size**: front notes drag from anywhere; a **zone drags only by its title strip or its edge**, so its inside stays the board (select tables, marquee, pan). With **Move cards with zone** (on for new zones) dragging a zone moves the tables inside it; Alt+drag moves the zone alone. Handles resize a selected note. Moves snap to 8 px (Shift: free). Arrows nudge (Shift: 8 px).
- **Text**: double-click a note to write in it; Esc cancels, Ctrl+Enter or a click outside keeps it. The format bar's 3×3 grid puts the text top / middle / bottom × left / centre / right.
- **Format bar** (above the selected notes): shape, 8 colours plus any colour and No fill, fill opacity, border solid / dashed / none, text size A− A+, bold, text position, Bring to front / Send to back, Move cards with zone, **lock**, duplicate, delete. Right-click a note: Edit text, Lock, Duplicate, Front / back, Delete.
- **Lock** (format bar, right-click, Ctrl+L): a locked note does not move or resize and a click on a locked zone's inside goes to the board; Delete asks first. Only the user locks and unlocks.
- **Auto zones**: one zone per group of joined tables (all joined: one per schema), each in its own colour with a label; running it again replaces the earlier auto zones, not the user's own notes.
- **You** can see the board with `read_board` (tables with their positions and sizes, joins with 1:N / N:M, notes with ids) and change it with `edit_board` (add / update / remove notes, one undo step for the user). You cannot move tables, lock or unlock, or touch a locked note. Put a zone around its tables with about 24 px of room and 28 px above them for its title; keep labels short and colours distinct.
