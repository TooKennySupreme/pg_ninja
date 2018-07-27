pg_ninja
##############

.. image:: https://img.shields.io/github/issues/transferwise/pg_ninja.svg   
  :target: https://github.com/transferwise/pg_ninja/issues
	
.. image:: https://img.shields.io/github/forks/transferwise/pg_ninja.svg   
  :target: https://github.com/transferwise/pg_ninja/network

.. image:: https://img.shields.io/github/stars/transferwise/pg_ninja.svg   
  :target: https://github.com/transferwise/pg_ninja/stargazers
  
.. image:: https://img.shields.io/badge/license-Apache%202-blue.svg   
  :target: https://raw.githubusercontent.com/transferwise/pg_ninja/master/LICENSE
  
pg_ninja is a tool for replicating and obfuscating the data from MySQL to PostgreSQL compatible with Python 3.3+. 
The system use the library mysql-replication to pull the row images from MySQL which are transformed into a jsonb object. 
A pl/pgsql function decodes the jsonb and replays the changes into the PostgreSQL database.

The tool requires an initial replica setup which pulls the data from MySQL locked in read only mode. 
This is done by the tool running FLUSH TABLE WITH READ LOCK; .

pg_ninja can pull the data from a cascading replica when the MySQL slave is configured with log-slave-updates.

Current version: 0.1 alpha1

