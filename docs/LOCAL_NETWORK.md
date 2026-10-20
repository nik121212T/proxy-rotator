# Partager le dashboard sur le réseau local

L’application doit rester démarrée sur le Mac. Les collègues rejoignent le
même réseau et ouvrent `http://ADRESSE_DU_MAC:PORT/login`.

Configuration dans `.env` (ne jamais publier ce fichier) :

```dotenv
BIND_HOST=0.0.0.0
ADMIN_PORT=5001
GATEWAY_BIND_HOST=127.0.0.1
ALLOW_REMOTE_BIND=true
ADMIN_ALLOWED_HOSTS='["192.168.1.51","Pc-du-future-3.local"]'
ADMIN_TOKEN=REMPLACER_PAR_UN_SECRET_ALEATOIRE
```

Adapter l’adresse, le nom et le port à la machine. Sur ce Mac, le port 5000
est actuellement occupé par Control Center/AirPlay ; choisir un autre port
ou désactiver AirPlay avant de le réutiliser. Éviter le port 6000, bloqué par
les navigateurs. Le pare-feu doit autoriser les connexions entrantes de Python.

La page `/login` enregistre le jeton dans un cookie HttpOnly. Le jeton protège
le dashboard, l’API et les événements WebSocket. Il donne accès à la même
instance d’administration : les collègues partagent le pool et les réglages,
sans comptes ni permissions individuelles. Les navigateurs lancés par
l’application s’ouvrent sur la machine hôte.

Les passerelles HTTP/SOCKS restent sur `127.0.0.1` avec la configuration ci-dessus.
Le partage concerne le dashboard uniquement. Ne pas ouvrir de redirection sur
le routeur Internet. Pour un réseau non fiable, ajouter HTTPS : le jeton circule
en clair avec HTTP. Si l’adresse DHCP change, mettre à jour les hôtes autorisés
et le lien ; le nom `.local` peut également fonctionner selon le poste client.
