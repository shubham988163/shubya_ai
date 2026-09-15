import 'package:flutter/material.dart';

class AlertsScreen extends StatelessWidget {
  const AlertsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Live Signals & Telegram Logs', style: TextStyle(fontWeight: FontWeight.bold)),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _buildAlertCard(
            title: 'Bullish Breakout: RELIANCE 3000 CE',
            time: '10:45 AM',
            message: 'Price crossed VWAP + High Volume Surge (2.4x avg). Target: 3040, SL: 2960',
            type: 'BUY',
          ),
          _buildAlertCard(
            title: 'Index Support Tested: NIFTY 25,300',
            time: '10:15 AM',
            message: 'Heavy Put Writing detected at 25,300 PE. Rebound expected towards 25,380.',
            type: 'INFO',
          ),
          _buildAlertCard(
            title: 'Bearish Breakdown: TCS 4500 PE',
            time: '09:40 AM',
            message: 'Rejection from 20 EMA with open interest addition on call side.',
            type: 'SELL',
          ),
        ],
      ),
    );
  }

  Widget _buildAlertCard({
    required String title,
    required String time,
    required String message,
    required String type,
  }) {
    Color badgeColor = type == 'BUY'
        ? const Color(0xFF10B981)
        : type == 'SELL'
            ? const Color(0xFFEF4444)
            : const Color(0xFF38BDF8);

    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFF1E293B),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: badgeColor.withOpacity(0.2)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: badgeColor.withOpacity(0.15),
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  type,
                  style: TextStyle(color: badgeColor, fontWeight: FontWeight.bold, fontSize: 11),
                ),
              ),
              Text(time, style: const TextStyle(color: Colors.white54, fontSize: 12)),
            ],
          ),
          const SizedBox(height: 8),
          Text(title, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14)),
          const SizedBox(height: 6),
          Text(message, style: const TextStyle(color: Colors.white70, fontSize: 13, height: 1.4)),
        ],
      ),
    );
  }
}
