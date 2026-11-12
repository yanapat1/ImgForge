---
title: The sample database (sample_shop)
summary: the example database every user gets on the first start: its tables, relationships, how to drop or restore it
high: true
---
- On the first start the app makes an example PostgreSQL database **sample_shop** on its own server, and each user finds the connection **Sample shop (example)** (marked *example* in the Objects panel) once; deleting the connection never brings it back. The first query tab of a new user opens on it with a starter query.
- Tables (schema public), each table and column described in the database (the Catalog shows them):
  - `customers` (200): id, name, email (unique), city, country, signed_up.
  - `customer_profiles` (150): customer_id (primary key and foreign key to customers: **1 : 0..1**), birth_year, loyalty_tier (bronze, silver, gold, platinum), marketing_opt_in.
  - `products` (40): id, sku (unique), name, category (Coffee, Tea, Equipment, Mugs, Snacks, Gifts), price.
  - `orders` (1 500, 2025-01 to 2026-10): id, customer_id (**many orders to one customer**), ordered_at, status (paid, shipped, delivered, cancelled, refunded), total (= the sum of its items).
  - `order_items` (~4 000): order_id, product_id, qty, unit_price; primary key (order_id, product_id): the bridge that makes orders and products **N : M**.
  - View `monthly_sales`: month, orders, revenue (without cancelled and refunded orders).
- It is the user's to change: they may update rows, `DROP TABLE order_items;`, or remove all of it with `DROP DATABASE sample_shop WITH (FORCE);` run with database **postgres** chosen on the toolbar. Changes still go through the confirm card (and `run_change` for you). It is **shared by everyone on this server**: say so before suggesting a drop.
- After a drop, the connection says the sample was removed. An **admin** restores it from that message (Restore sample database) or with `python -m dataviz data-sample reset`; `status` and `remove` are the other commands. A restore brings back the first day's rows.
- Good for practice: joins (orders ↔ customers, orders ↔ products through order_items), group by month, the Visual builder's 1:N / N:M ends and zones, Analyze and Forecast on monthly_sales.
