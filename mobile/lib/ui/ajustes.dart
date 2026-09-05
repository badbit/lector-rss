/// Pantalla de ajustes: conexión con el hub.
library;

import 'package:flutter/material.dart';

import '../app_state.dart';
import '../background.dart';

class AjustesPage extends StatefulWidget {
  const AjustesPage({super.key, required this.estado});

  final AppState estado;

  @override
  State<AjustesPage> createState() => _AjustesPageState();
}

class _AjustesPageState extends State<AjustesPage> {
  late final TextEditingController _url =
      TextEditingController(text: widget.estado.hubUrl);
  late final TextEditingController _token =
      TextEditingController(text: widget.estado.hubToken);
  late int _dias = widget.estado.ventanaDias;
  late bool _segundoPlano = widget.estado.sincronizacionEnSegundoPlano;
  bool _probando = false;
  String? _resultado;
  bool _correcto = false;

  @override
  void dispose() {
    _url.dispose();
    _token.dispose();
    super.dispose();
  }

  Future<void> _probar() async {
    setState(() {
      _probando = true;
      _resultado = null;
    });
    final ok = await widget.estado.probarConexion(_url.text, _token.text);
    if (!mounted) return;
    setState(() {
      _probando = false;
      _correcto = ok;
      _resultado = ok
          ? 'Conectado con el hub'
          : (widget.estado.ultimoError ?? 'Sin respuesta');
    });
  }

  Future<void> _guardar() async {
    await widget.estado.guardarAjustes(
      url: _url.text,
      token: _token.text,
      dias: _dias,
      segundoPlano: _segundoPlano,
    );
    await configurarSincronizacionEnSegundoPlano(_segundoPlano);
    if (!mounted) return;
    Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Ajustes')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _url,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Dirección del hub',
              hintText: 'http://hub.tu-tailnet.ts.net:8787',
              helperText: 'El hub no se expone a Internet: se llega por la VPN',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _token,
            obscureText: true,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Token',
              helperText: 'Uno de los hub.tokens del config.yaml',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 24),
          Text('Ventana de sincronización',
              style: Theme.of(context).textTheme.titleSmall),
          const Text(
            'El archivo del hub es permanente. El móvil solo replica los últimos '
            'días, más lo guardado y lo que esté sin leer.',
            style: TextStyle(fontSize: 12, color: Colors.grey),
          ),
          const SizedBox(height: 8),
          SegmentedButton<int>(
            segments: const [
              ButtonSegment(value: 7, label: Text('7 días')),
              ButtonSegment(value: 30, label: Text('30 días')),
              ButtonSegment(value: 90, label: Text('90 días')),
            ],
            selected: {_dias},
            onSelectionChanged: (s) => setState(() => _dias = s.first),
          ),
          const SizedBox(height: 16),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Sincronizar en segundo plano'),
            subtitle: const Text(
                'Android intentará sincronizar cada 30 minutos cuando haya red.'),
            value: _segundoPlano,
            onChanged: (value) => setState(() => _segundoPlano = value),
          ),
          const SizedBox(height: 24),
          Row(
            children: [
              OutlinedButton.icon(
                onPressed: _probando ? null : _probar,
                icon: _probando
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.wifi_tethering),
                label: const Text('Probar'),
              ),
              const SizedBox(width: 12),
              FilledButton(onPressed: _guardar, child: const Text('Guardar')),
            ],
          ),
          if (_resultado != null) ...[
            const SizedBox(height: 16),
            Row(
              children: [
                Icon(_correcto ? Icons.check_circle : Icons.error,
                    color: _correcto ? Colors.green : Colors.red, size: 18),
                const SizedBox(width: 8),
                Expanded(child: Text(_resultado!)),
              ],
            ),
          ],
          const Divider(height: 48),
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.delete_outline),
            title: const Text('Borrar la copia local'),
            subtitle: const Text(
                'Vacía el espejo del móvil y vuelve a traérselo del hub. '
                'Los cambios sin subir se pierden.'),
            onTap: () async {
              final confirmar = await showDialog<bool>(
                context: context,
                builder: (c) => AlertDialog(
                  title: const Text('¿Borrar la copia local?'),
                  content: const Text(
                      'Se vaciará todo lo descargado. Lo que no se haya subido '
                      'todavía al hub se perderá.'),
                  actions: [
                    TextButton(
                        onPressed: () => Navigator.pop(c, false),
                        child: const Text('Cancelar')),
                    FilledButton(
                        onPressed: () => Navigator.pop(c, true),
                        child: const Text('Borrar')),
                  ],
                ),
              );
              if (confirmar == true) await widget.estado.olvidarTodo();
            },
          ),
        ],
      ),
    );
  }
}
