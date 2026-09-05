/// Cliente Android del lector RSS.
///
/// Es un cliente delgado: no descarga feeds ni genera EPUB. Todo eso vive en el
/// hub. La app lista, lee, marca y sincroniza, y funciona sin conexión.
library;

import 'package:flutter/material.dart';
import 'package:intl/date_symbol_data_local.dart';

import 'app_state.dart';
import 'background.dart';
import 'ui/inicio.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initializeDateFormatting('es');
  final estado = await AppState.crear();
  await configurarSincronizacionEnSegundoPlano(
      estado.sincronizacionEnSegundoPlano);
  runApp(RssApp(estado: estado));
}

class RssApp extends StatelessWidget {
  const RssApp({super.key, required this.estado});

  final AppState estado;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Lector RSS',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFFE8663D),
        useMaterial3: true,
      ),
      darkTheme: ThemeData(
        colorSchemeSeed: const Color(0xFFE8663D),
        brightness: Brightness.dark,
        useMaterial3: true,
      ),
      home: ListenableBuilder(
        listenable: estado,
        builder: (context, _) => InicioPage(estado: estado),
      ),
    );
  }
}
