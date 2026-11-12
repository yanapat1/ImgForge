---
name: explain-board
when: the user wants the visual builder board organised, explained or made easier to read (zones, colours, labels, notes)
description: Group the board's tables into labelled zones and add short notes on the key joins
---
1. `read_board`. With no tables on it, tell the user to drag tables onto the Visual builder first and stop.
2. Group the tables by meaning: the joins and foreign keys first, then names (orders, order_items → Sales). At most 6 groups.
3. `edit_board` once: for each group one zone (layer back, a distinct colour, alpha about 0.14, dashed border) around its
   tables: 24 px of room on each side and 28 px above for the title, with a short label as its text (top left, bold).
4. In the same call, a few sticky notes (round, yellow, alpha 1, front, size 13) beside the joins that matter: what a 1:N
   means here, and a warning on any N:M. Keep the whole board under 12 notes, and say in the call's note what you did.
5. Never remove or change the user's own notes unless they asked; locked notes cannot be changed. Tell the user that
   Ctrl+Z on the board undoes it, and that Lock keeps a zone in place.
