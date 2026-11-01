import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

// Adresse de ton application en ligne (sans "/" final)
const String baseUrl = 'https://livraison-demo.onrender.com';

void main() => runApp(const MyApp());

class MyApp extends StatelessWidget {
  const MyApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Livreur',
        theme: ThemeData(colorSchemeSeed: const Color(0xFF1F3A5F), useMaterial3: true),
        home: const LoginPage(),
      );
}

class LoginPage extends StatefulWidget {
  const LoginPage({super.key});
  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final user = TextEditingController();
  final pwd = TextEditingController();
  String erreur = '';

  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((p) {
      final t = p.getString('token');
      if (t != null && mounted) {
        Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => OrdersPage(token: t)));
      }
    });
  }

  Future<void> connexion() async {
    try {
      final r = await http.post(Uri.parse('$baseUrl/api/livreur/login'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'username': user.text.trim(), 'password': pwd.text}));
      if (r.statusCode == 200) {
        final token = jsonDecode(r.body)['token'] as String;
        (await SharedPreferences.getInstance()).setString('token', token);
        if (!mounted) return;
        Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => OrdersPage(token: token)));
      } else {
        setState(() => erreur = 'Identifiants incorrects');
      }
    } catch (_) {
      setState(() => erreur = 'Connexion impossible');
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Connexion livreur')),
        body: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(children: [
            TextField(controller: user, decoration: const InputDecoration(labelText: 'Identifiant')),
            TextField(controller: pwd, obscureText: true, decoration: const InputDecoration(labelText: 'Mot de passe')),
            const SizedBox(height: 16),
            FilledButton(onPressed: connexion, child: const Text('Se connecter')),
            Text(erreur, style: const TextStyle(color: Colors.red)),
          ]),
        ),
      );
}

class OrdersPage extends StatefulWidget {
  final String token;
  const OrdersPage({super.key, required this.token});
  @override
  State<OrdersPage> createState() => _OrdersPageState();
}

class _OrdersPageState extends State<OrdersPage> {
  List<dynamic> commandes = [];
  Timer? timer;

  Map<String, String> get headers =>
      {'Content-Type': 'application/json', 'Authorization': 'Bearer ${widget.token}'};

  @override
  void initState() {
    super.initState();
    charger();
    envoyerPosition();
    timer = Timer.periodic(const Duration(seconds: 20), (_) => envoyerPosition());
  }

  @override
  void dispose() {
    timer?.cancel();
    super.dispose();
  }

  Future<void> charger() async {
    final r = await http.get(Uri.parse('$baseUrl/api/livreur/commandes'), headers: headers);
    if (r.statusCode == 401) return deconnexion();
    if (r.statusCode == 200 && mounted) setState(() => commandes = jsonDecode(r.body));
  }

  Future<void> envoyerPosition() async {
    try {
      var perm = await Geolocator.checkPermission();
      if (perm == LocationPermission.denied) perm = await Geolocator.requestPermission();
      if (perm == LocationPermission.denied || perm == LocationPermission.deniedForever) return;
      final p = await Geolocator.getCurrentPosition();
      await http.post(Uri.parse('$baseUrl/api/livreur/position'),
          headers: headers, body: jsonEncode({'lat': p.latitude, 'lng': p.longitude}));
    } catch (_) {}
  }

  Future<void> changerStatut(int id, String statut) async {
    await http.post(Uri.parse('$baseUrl/api/livreur/commandes/$id/statut'),
        headers: headers, body: jsonEncode({'statut': statut}));
    await charger();
  }

  Future<void> deconnexion() async {
    (await SharedPreferences.getInstance()).remove('token');
    if (!mounted) return;
    Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const LoginPage()));
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Mes livraisons'), actions: [
          IconButton(onPressed: charger, icon: const Icon(Icons.refresh)),
          IconButton(onPressed: deconnexion, icon: const Icon(Icons.logout)),
        ]),
        body: RefreshIndicator(
          onRefresh: charger,
          child: commandes.isEmpty
              ? ListView(children: const [Padding(padding: EdgeInsets.all(24), child: Text('Aucune livraison.'))])
              : ListView(
                  children: commandes
                      .map((c) => Card(
                            margin: const EdgeInsets.all(8),
                            child: Padding(
                              padding: const EdgeInsets.all(12),
                              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                                Text('#${c['id']} ${c['client_nom']}', style: const TextStyle(fontWeight: FontWeight.bold)),
                                Text('${c['client_tel']}'),
                                Text('${c['adresse']} — ${c['prix']} GNF'),
                                Text('Statut : ${c['statut']}'),
                                Row(children: [
                                  TextButton(onPressed: () => changerStatut(c['id'], 'en cours'), child: const Text('Démarrer')),
                                  FilledButton(onPressed: () => changerStatut(c['id'], 'livré'), child: const Text('Livré')),
                                ]),
                              ]),
                            ),
                          ))
                      .toList()),
        ),
      );
}
