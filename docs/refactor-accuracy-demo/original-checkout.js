const express = require('express');
const router = express.Router();
// Assume db is a standard MongoDB or PostgreSQL client instance
const db = require('../db'); 
const jwt = require('jsonwebtoken');

// FEATURE: Checkout a shopping cart and deduct inventory
router.post('/checkout', async (req, res) => {
  try {
    const authHeader = req.headers.authorization;
    if (!authHeader) return res.status(401).json({ error: 'Missing token' });

    // 1. JWT verification using a legacy structure
    const token = authHeader.split(' ')[1];
    const user = jwt.verify(token, process.env.JWT_SECRET, { algorithms: ['HS256'] });

    const { cartItems, discountCode } = req.body;
    let finalPrice = 0;

    // 2. Vulnerable loop logic & Race Condition
    for (const item of cartItems) {
      const product = await db.query('SELECT * FROM products WHERE id = $1', [item.id]);
      
      // Check stock
      if (product.stock < item.quantity) {
        return res.status(400).json({ error: `Out of stock: ${product.name}` });
      }

      finalPrice += product.price * item.quantity;

      // Deduct stock (Race condition danger zone if two users buy simultaneously!)
      const newStock = product.stock - item.quantity;
      await db.query('UPDATE products SET stock = $1 WHERE id = $2', [newStock, item.id]);
    }

    // 3. Outdated third-party API implementation (Simulating Stripe legacy API)
    // Assume we use an old stripe package where this structural payload causes a failure
    if (discountCode === 'SUMMER50') {
      finalPrice = finalPrice * 0.5;
    }

    // Return success
    res.status(200).json({ success: true, totalPaid: finalPrice });

  } catch (error) {
    // 4. Bad Error Handling: Will crash the server if headers were already sent in the loop
    res.status(500).json({ error: error.message });
  }
});

module.exports = router;
