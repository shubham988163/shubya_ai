import 'package:flutter/material.dart';

class OptionsChainScreen extends StatelessWidget {
  const OptionsChainScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('F&O Options Chain', style: TextStyle(fontWeight: FontWeight.bold)),
        actions: [
          PopupMenuButton<String>(
            initialValue: 'NIFTY',
            itemBuilder: (context) => [
              const PopupMenuItem(value: 'NIFTY', child: Text('NIFTY 50')),
              const PopupMenuItem(value: 'BANKNIFTY', child: Text('BANKNIFTY')),
              const PopupMenuItem(value: 'FINNIFTY', child: Text('FINNIFTY')),
            ],
            child: Container(
              margin: const EdgeInsets.symmetric(horizontal: 12),
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
              decoration: BoxDecoration(
                color: const Color(0xFF0F172A),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.white24),
              ),
              child: const Row(
                children: [
                  Text('NIFTY', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
                  Icon(Icons.arrow_drop_down, size: 18),
                ],
              ),
            ),
          ),
        ],
      ),
      body: Column(
        children: [
          // Header Row
          Container(
            padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 8),
            color: const Color(0xFF1E293B),
            child: const Row(
              children: [
                Expanded(child: Text('CALL OI / LTP', textAlign: TextAlign.left, style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: Colors.white60))),
                Text('STRIKE', style: TextStyle(fontSize: 12, fontWeight: FontWeight.bold, color: Color(0xFF38BDF8))),
                Expanded(child: Text('PUT OI / LTP', textAlign: TextAlign.right, style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold, color: Colors.white60))),
              ],
            ),
          ),
          const Divider(height: 1, color: Colors.black26),
          // Options Rows
          Expanded(
            child: ListView(
              children: [
                _buildStrikeRow(callOI: '45.2L', callLtp: '₹142.50', strike: '25,200', putLtp: '₹48.20', putOI: '92.1L', isAtm: false),
                _buildStrikeRow(callOI: '58.7L', callLtp: '₹105.00', strike: '25,250', putLtp: '₹62.40', putOI: '78.5L', isAtm: false),
                _buildStrikeRow(callOI: '84.3L', callLtp: '₹75.20', strike: '25,300', putLtp: '₹82.10', putOI: '85.2L', isAtm: true),
                _buildStrikeRow(callOI: '98.5L', callLtp: '₹51.00', strike: '25,350', putLtp: '₹108.60', putOI: '42.0L', isAtm: false),
                _buildStrikeRow(callOI: '112.0L', callLtp: '₹34.40', strike: '25,400', putLtp: '₹140.00', putOI: '28.4L', isAtm: false),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildStrikeRow({
    required String callOI,
    required String callLtp,
    required String strike,
    required String putLtp,
    required String putOI,
    required bool isAtm,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 12),
      decoration: BoxDecoration(
        color: isAtm ? const Color(0xFF38BDF8).withOpacity(0.08) : Colors.transparent,
        border: Border(bottom: BorderSide(color: Colors.white.withOpacity(0.05))),
      ),
      child: Row(
        children: [
          Expanded(
            child: Row(
              children: [
                Text(callOI, style: const TextStyle(fontSize: 11, color: Colors.white54)),
                const Spacer(),
                Text(callLtp, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold, color: Color(0xFF10B981))),
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
            margin: const EdgeInsets.symmetric(horizontal: 8),
            decoration: BoxDecoration(
              color: isAtm ? const Color(0xFF38BDF8).withOpacity(0.2) : const Color(0xFF1E293B),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text(
              strike,
              style: TextStyle(
                fontWeight: FontWeight.bold,
                fontSize: 13,
                color: isAtm ? const Color(0xFF38BDF8) : Colors.white,
              ),
            ),
          ),
          Expanded(
            child: Row(
              children: [
                Text(putLtp, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold, color: Color(0xFFEF4444))),
                const Spacer(),
                Text(putOI, style: const TextStyle(fontSize: 11, color: Colors.white54)),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
