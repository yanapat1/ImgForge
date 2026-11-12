---
name: explain-board
when: the user wants the visual builder board organised, explained or made easier to read (zones, colours, labels, notes)
description: Group the board's tables into labelled zones, pin a short description on each table, and note the key joins
---
1. `read_board`. With no tables on it, tell the user to drag tables onto the Visual builder first and stop. A table's
   description there is the catalog's; without one, write it from the table and column names (or `describe_table`).
2. Group the tables by meaning: the joins and foreign keys first, then names (orders, order_items → Sales). At most 6 groups.
3. ONE `edit_board` call, and keep the thinking short: you never compute coordinates, the page measures the cards.
   - a describe op per table the user wants explained: op describe, table = its id, text = one or two short sentences
     (what one row is, what it is for). It sits above the card and moves with it; describing a table again rewrites it.
   - a zone op per group: op zone, label = 2-4 words, tables = the table ids. Colours differ on their own.
   - arrange true when tables of different groups are mixed on the board (zones would overlap) or the user asks to tidy
     it: the page moves the cards group by group (the SQL does not change).
   - optionally a sticky note (op add, note: shape round, fill yellow, alpha 1, layer front, size 13, with x, y next to
     the join) for a 1:N worth explaining or a warning on an N:M. Keep the board under 12 notes plus the descriptions.
4. If the result lists zones_overlap, call `edit_board` again with the same zone ops and arrange true.
5. Never remove or change the user's own notes unless they asked; locked notes cannot be changed. Tell the user that
   Ctrl+Z on the board undoes it, and that Lock keeps a zone in place.
