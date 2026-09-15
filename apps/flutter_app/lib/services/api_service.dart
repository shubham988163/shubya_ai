import 'dart:convert';
import 'package:http/http.dart' as http;

class ApiService {
  // Replace with local IP or production API endpoint
  static const String baseUrl = 'http://127.0.0.1:8000';

  static Future<Map<String, dynamic>> fetchMarketOverview() async {
    try {
      final response = await http.get(Uri.parse('$baseUrl/api/dashboard/overview'));
      if (response.statusCode == 200) {
        return json.decode(response.body);
      }
    } catch (e) {
      // Fallback or offline mock
    }
    return {};
  }

  static Future<List<dynamic>> fetchOptionsChain(String symbol) async {
    try {
      final response = await http.get(Uri.parse('$baseUrl/api/options/chain?symbol=$symbol'));
      if (response.statusCode == 200) {
        return json.decode(response.body);
      }
    } catch (e) {
      // Fallback
    }
    return [];
  }
}
